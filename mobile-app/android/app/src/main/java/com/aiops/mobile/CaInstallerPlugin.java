package com.aiops.mobile;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.provider.Settings;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

/**
 * 平台根证书一键引导安装（自签名 HTTPS 场景）。
 *
 * 说明：Android 不允许应用静默写入系统信任库，此插件仅做「引导」——
 * 将内置在 res/raw/aiops_root_ca.crt 的平台根 CA 导出到应用缓存，
 * 通过系统 CertInstaller（application/x-x509-ca-cert → CA 证书分支）唤起
 * 系统证书安装界面，用户按系统流程确认 1~2 次即可完成安装。
 *
 * 前端调用：Capacitor.Plugins.CaInstaller.installRootCertificate()
 */
@CapacitorPlugin(name = "CaInstaller")
public class CaInstallerPlugin extends Plugin {

    @PluginMethod
    public void installRootCertificate(PluginCall call) {
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("activity not ready");
            return;
        }
        try {
            // 1. 将内置根 CA 导出到应用缓存目录
            File caFile = new File(activity.getCacheDir(), "aiops_root_ca.crt");
            try (InputStream in = activity.getResources().openRawResource(R.raw.aiops_root_ca);
                 FileOutputStream out = new FileOutputStream(caFile)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                }
            }

            // 2. 通过 FileProvider 暴露 content:// URI
            Uri uri = FileProvider.getUriForFile(
                    activity,
                    activity.getPackageName() + ".fileprovider",
                    caFile);

            // 3. 唤起系统证书安装（CA 证书分支）
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(uri, "application/x-x509-ca-cert");
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            try {
                activity.startActivity(intent);
                call.resolve(new JSObject().put("ok", true).put("action", "cert_installer"));
                return;
            } catch (ActivityNotFoundException e) {
                // 4. 部分 ROM 无 CertInstaller handler → 退回系统安全设置页
            }
            try {
                Intent fallback = new Intent(Settings.ACTION_SECURITY_SETTINGS);
                fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                activity.startActivity(fallback);
                call.resolve(new JSObject().put("ok", true).put("action", "security_settings"));
            } catch (ActivityNotFoundException ex) {
                call.reject("当前设备无证书安装入口: " + ex.getMessage());
            }
        } catch (Exception e) {
            call.reject("导出证书失败: " + e.getMessage());
        }
    }
}
