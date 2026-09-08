package com.aiops.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.Certificate;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLException;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * AiopConnector —— 通用 HTTPS 原生网络通道 + TOFU（Trust On First Use）指纹信任。
 *
 * 背景：移动端 SPA 打包在 APK 本地加载，远程 API 均为 WebView 内 XHR；自签名平台证书
 * 无法被 WebView 网络栈信任（targetSdk>=24 只信系统 CA），且 onReceivedSslError 管不到
 * XHR。故所有业务请求改走本插件（HttpsURLConnection），按 host:port 持久化「首次连接时
 * 用户确认的叶子证书 SHA-256 指纹」：指纹命中即信任（hostname 校验因指纹已锁定身份而放行），
 * 无记录返回 FIRST_USE（附指纹供前端确认），指纹不符返回 MISMATCH。
 *
 * 调用（前端 axios 自定义 adapter 统一使用）：
 *   Capacitor.Plugins.AiopConnector.request({url, method, headers, body, parts, boundary, timeout})
 *   Capacitor.Plugins.AiopConnector.probe({host, port})
 *   Capacitor.Plugins.AiopConnector.pin({host, port, fingerprint})
 *   Capacitor.Plugins.AiopConnector.unpin({host, port})
 *   Capacitor.Plugins.AiopConnector.listPins()
 */
@CapacitorPlugin(name = "AiopConnector")
public class AiopConnectorPlugin extends Plugin {

    private static final String TAG = "AiopConnector";
    private static final String PREFS = "aiops_tofu_pins";
    private static final String ERR_FIRST_USE = "TOFU_FIRST_USE";
    private static final String ERR_MISMATCH = "TOFU_MISMATCH";
    private static final String ERR_NETWORK = "NETWORK_ERROR";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    // ---------------- TOFU pin 存储 ----------------

    private SharedPreferences prefs() {
        return getContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** host:port → "AA:BB:...:FF"（大写、冒号分隔） */
    private static String hostKey(String host, int port) {
        String h = host == null ? "" : host.trim().toLowerCase();
        while (h.endsWith(".")) h = h.substring(0, h.length() - 1);
        return h + ":" + port;
    }

    private static String normalizeFp(String fp) {
        return fp == null ? "" : fp.replace(":", "").replace(" ", "").toUpperCase();
    }

    private static String prettyFp(String fp) {
        String n = normalizeFp(fp);
        if (n.length() != 64) return fp == null ? "" : fp;
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 64; i += 2) {
            if (i > 0) sb.append(':');
            sb.append(n, i, i + 2);
        }
        return sb.toString();
    }

    private static String sha256Fingerprint(X509Certificate cert) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] digest = md.digest(cert.getEncoded());
        StringBuilder sb = new StringBuilder();
        for (byte b : digest) sb.append(String.format("%02X", b));
        return prettyFp(sb.toString());
    }

    // ---------------- TLS 握手探测（trust-all，仅取指纹，不发业务数据） ----------------

    private static SSLContext trustAllContext() throws Exception {
        SSLContext sc = SSLContext.getInstance("TLS");
        sc.init(null, new TrustManager[]{new X509TrustManager() {
            public void checkClientTrusted(X509Certificate[] chain, String authType) {}
            public void checkServerTrusted(X509Certificate[] chain, String authType) {}
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
        }}, new SecureRandom());
        return sc;
    }

    private JSObject probeTls(String host, int port, int timeoutMs) {
        JSObject out = new JSObject();
        SSLSocket sock = null;
        try {
            SSLSocketFactory f = trustAllContext().getSocketFactory();
            sock = (SSLSocket) f.createSocket();
            sock.connect(new InetSocketAddress(host, port), Math.max(timeoutMs, 5000));
            sock.setSoTimeout(Math.max(timeoutMs, 5000));
            sock.startHandshake();
            Certificate[] certs = sock.getSession().getPeerCertificates();
            if (certs == null || certs.length == 0) {
                out.put("ok", false);
                out.put("code", ERR_NETWORK);
                out.put("message", "服务器未返回证书");
                return out;
            }
            X509Certificate leaf = (X509Certificate) certs[0];
            out.put("ok", true);
            out.put("host", host);
            out.put("port", port);
            out.put("fingerprint", sha256Fingerprint(leaf));
            out.put("subject", leaf.getSubjectX500Principal().getName());
            out.put("issuer", leaf.getIssuerX500Principal().getName());
            out.put("notAfter", leaf.getNotAfter().getTime());
            return out;
        } catch (Exception e) {
            out.put("ok", false);
            out.put("code", ERR_NETWORK);
            out.put("message", (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()));
            return out;
        } finally {
            if (sock != null) {
                try { sock.close(); } catch (IOException ignored) {}
            }
        }
    }

    // ---------------- 固定指纹的 SSLSocketFactory ----------------

    private SSLSocketFactory pinnedFactory(final String hostKey, final String pin) throws Exception {
        SSLContext sc = SSLContext.getInstance("TLS");
        sc.init(null, new TrustManager[]{new X509TrustManager() {
            public void checkClientTrusted(X509Certificate[] chain, String authType) {}
            public void checkServerTrusted(X509Certificate[] chain, String authType) throws CertificateException {
                if (chain == null || chain.length == 0) throw new CertificateException("no cert");
                String fp = sha256FingerprintSafe(chain[0]);
                if (fp == null || !normalizeFp(fp).equals(normalizeFp(pin))) {
                    throw new CertificateException("TOFU_PIN_MISMATCH host=" + hostKey);
                }
            }
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
        }}, new SecureRandom());
        return sc.getSocketFactory();
    }

    private static String sha256FingerprintSafe(X509Certificate cert) {
        try { return sha256Fingerprint(cert); } catch (Exception e) { return null; }
    }

    // ---------------- HTTP 请求 ----------------

    private JSObject doRequest(JSObject args) {
        JSObject out = new JSObject();
        String urlStr = args.optString("url", "");
        String method = (args.optString("method", "GET")).toUpperCase();
        JSONObject headersObj = args.optJSONObject("headers");
        String body = args.optString("body", null);
        JSONArray parts = args.optJSONArray("parts");
        String boundary = args.optString("boundary", "");
        int timeoutMs = args.optInt("timeout", 20000);

        HttpURLConnection conn = null;
        try {
            URL url = new URL(urlStr);
            if (!"https".equalsIgnoreCase(url.getProtocol())) {
                out.put("ok", false);
                out.put("code", ERR_NETWORK);
                out.put("message", "仅支持 https 连接");
                return out;
            }
            String host = url.getHost();
            int port = url.getPort() == -1 ? url.getDefaultPort() : url.getPort();
            String hkey = hostKey(host, port);
            Log.i(TAG, "request " + method + " " + urlStr + " host=" + host + " port=" + port);

            // TOFU：无 pin → 探测指纹并返回 FIRST_USE（不发业务请求）
            String pin = prefs().getString(hkey, null);
            Log.i(TAG, "pin[" + hkey + "]=" + (pin == null ? "NULL(首次)" : "SET"));
            if (pin == null) {
                JSObject probe = probeTls(host, port, timeoutMs);
                Log.i(TAG, "probeTls ok=" + probe.optBoolean("ok", false) + " code=" + probe.optString("code", ""));
                if (probe.optBoolean("ok", false)) {
                    out.put("ok", false);
                    out.put("code", ERR_FIRST_USE);
                    out.put("host", host);
                    out.put("port", port);
                    out.put("fingerprint", probe.getString("fingerprint"));
                    out.put("subject", probe.optString("subject", ""));
                    out.put("issuer", probe.optString("issuer", ""));
                    out.put("notAfter", probe.optLong("notAfter", 0));
                    out.put("message", "首次连接，需要确认服务器证书指纹");
                } else {
                    out.put("ok", false);
                    out.put("code", ERR_NETWORK);
                    out.put("message", probe.optString("message", "TLS 握手失败"));
                }
                return out;
            }

            // 有 pin → 固定指纹建连
            conn = (HttpURLConnection) url.openConnection();
            if (conn instanceof HttpsURLConnection) {
                HttpsURLConnection https = (HttpsURLConnection) conn;
                try {
                    https.setSSLSocketFactory(pinnedFactory(hkey, pin));
                    Log.i(TAG, "pinnedFactory OK host=" + host);
                } catch (Exception fe) {
                    Log.e(TAG, "pinnedFactory FAIL: " + fe.getMessage());
                    throw fe;
                }
                https.setHostnameVerifier(new HostnameVerifier() {
                    public boolean verify(String h, javax.net.ssl.SSLSession s) { return true; }
                });
            }
            conn.setInstanceFollowRedirects(false);
            conn.setRequestMethod(method);
            conn.setConnectTimeout(Math.min(timeoutMs, 15000));
            conn.setReadTimeout(timeoutMs);

            if (headersObj != null) {
                Iterator<String> it = headersObj.keys();
                while (it.hasNext()) {
                    String k = it.next();
                    conn.setRequestProperty(k, headersObj.optString(k));
                }
            }

            // 请求体：JSON 文本 或 multipart（parts）
            if (parts != null && parts.length() > 0) {
                byte[] multipart = buildMultipart(parts, boundary);
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
                conn.setFixedLengthStreamingMode(multipart.length);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(multipart);
                }
            } else if (body != null && !body.isEmpty()) {
                byte[] payload = body.getBytes(StandardCharsets.UTF_8);
                conn.setDoOutput(true);
                conn.setFixedLengthStreamingMode(payload.length);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(payload);
                }
            }

            int status = conn.getResponseCode();
            Log.i(TAG, "HTTP " + status + " for " + method + " " + urlStr);
            InputStream is = status >= 400 ? conn.getErrorStream() : conn.getInputStream();
            String respBody = readAll(is);

            out.put("ok", true);
            out.put("status", status);
            out.put("body", respBody == null ? "" : respBody);
            JSObject respHeaders = new JSObject();
            Map<String, java.util.List<String>> hfs = conn.getHeaderFields();
            if (hfs != null) {
                for (Map.Entry<String, java.util.List<String>> e : hfs.entrySet()) {
                    String k = e.getKey();
                    if (k != null && e.getValue() != null && !e.getValue().isEmpty()) {
                        respHeaders.put(k, e.getValue().get(0));
                    }
                }
            }
            out.put("headers", respHeaders);
            return out;
        } catch (SSLException e) {
            Log.e(TAG, "SSLException: " + e.getMessage());
            // pin 已存在但握手失败（服务器换证书 / 中间人 / 网络异常）→ 探测当前指纹供比对
            String host = null;
            int p = -1;
            String pinNow = null;
            boolean probeOk = false;
            String probeFp = "";
            try {
                URL u = new URL(urlStr);
                host = u.getHost();
                p = u.getPort() == -1 ? u.getDefaultPort() : u.getPort();
                pinNow = prefs().getString(hostKey(host, p), null);
                JSObject probe = probeTls(host, p, timeoutMs);
                probeOk = probe.optBoolean("ok", false);
                if (probeOk) probeFp = probe.optString("fingerprint", "");
            } catch (Exception ex) {
                /* host/port 解析或探测失败，继续走兜底分支 */
            }
            out.put("ok", false);
            if (probeOk) {
                // 真·指纹不一致（服务器换证书 / 中间人）
                out.put("code", ERR_MISMATCH);
                out.put("message", "服务器证书指纹与已信任的不一致");
                out.put("fingerprint", probeFp);
            } else {
                // 已信任服务器但当前连不上（网络/防火墙/服务挂了）→ 不弹"指纹变化"误导用户
                out.put("code", ERR_NETWORK);
                out.put("message", "已信任服务器当前不可达：" + e.getMessage());
                out.put("fingerprint", "");
            }
            if (host != null) out.put("host", host);
            if (p > 0) out.put("port", p);
            out.put("pinnedFingerprint", pinNow == null ? "" : pinNow);
            return out;
        } catch (Exception e) {
            Log.e(TAG, "doRequest EX: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            out.put("ok", false);
            out.put("code", ERR_NETWORK);
            out.put("message", (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()));
            return out;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private byte[] buildMultipart(JSONArray parts, String boundary) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        String b = "--" + boundary;
        for (int i = 0; i < parts.length(); i++) {
            JSONObject part = parts.getJSONObject(i);
            String name = part.optString("name", "");
            String filename = part.optString("filename", null);
            String type = part.optString("type", "application/octet-stream");
            String data = part.optString("data", null); // base64

            StringBuilder head = new StringBuilder();
            head.append(b).append("\r\n");
            head.append("Content-Disposition: form-data; name=\"").append(name).append("\"");
            if (filename != null && !filename.isEmpty()) {
                head.append("; filename=\"").append(filename.replace("\"", "_").replace("\r", "").replace("\n", "")).append("\"");
            }
            head.append("\r\n");
            head.append("Content-Type: ").append(type).append("\r\n\r\n");
            bos.write(head.toString().getBytes(StandardCharsets.UTF_8));
            if (data != null && !data.isEmpty()) {
                byte[] raw = android.util.Base64.decode(data, android.util.Base64.DEFAULT);
                bos.write(raw);
            }
            bos.write("\r\n".getBytes(StandardCharsets.UTF_8));
        }
        bos.write((b + "--\r\n").getBytes(StandardCharsets.UTF_8));
        return bos.toByteArray();
    }

    private String readAll(InputStream is) throws IOException {
        if (is == null) return "";
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    // ---------------- Capacitor 方法 ----------------

    @PluginMethod
    public void request(final PluginCall call) {
        final JSObject args = call.getData();
        executor.execute(() -> {
            JSObject result = doRequest(args);
            call.resolve(result);
        });
    }

    @PluginMethod
    public void probe(final PluginCall call) {
        final String host = call.getString("host", "");
        final int port = call.getInt("port", 443);
        final int timeout = call.getInt("timeout", 10000);
        executor.execute(() -> {
            if (host.isEmpty()) {
                call.reject("host 不能为空");
                return;
            }
            call.resolve(probeTls(host, port, timeout));
        });
    }

    @PluginMethod
    public void pin(final PluginCall call) {
        final String host = call.getString("host", "");
        final int port = call.getInt("port", 443);
        final String fp = call.getString("fingerprint", "");
        if (host.isEmpty() || fp.isEmpty()) {
            call.reject("host 与 fingerprint 不能为空");
            return;
        }
        prefs().edit().putString(hostKey(host, port), prettyFp(fp)).apply();
        JSObject out = new JSObject();
        out.put("ok", true);
        out.put("host", host);
        out.put("port", port);
        call.resolve(out);
    }

    @PluginMethod
    public void unpin(final PluginCall call) {
        final String host = call.getString("host", "");
        final int port = call.getInt("port", 443);
        prefs().edit().remove(hostKey(host, port)).apply();
        JSObject out = new JSObject();
        out.put("ok", true);
        call.resolve(out);
    }

    @PluginMethod
    public void listPins(PluginCall call) {
        Map<String, ?> all = prefs().getAll();
        JSArray arr = new JSArray();
        for (Map.Entry<String, ?> e : all.entrySet()) {
            JSObject o = new JSObject();
            o.put("host", e.getKey());
            o.put("fingerprint", String.valueOf(e.getValue()));
            arr.put(o);
        }
        JSObject out = new JSObject();
        out.put("pins", arr);
        call.resolve(out);
    }
}
