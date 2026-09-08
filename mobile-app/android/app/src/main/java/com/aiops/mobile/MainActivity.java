package com.aiops.mobile;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // 平台根证书一键引导（自签名 HTTPS 部署场景，降级入口保留）
        registerPlugin(CaInstallerPlugin.class);
        // TOFU 通用 HTTPS 网络通道（连接任意自部署平台，首次指纹确认，零系统级装证书）
        registerPlugin(AiopConnectorPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
