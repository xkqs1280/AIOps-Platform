package com.aiops.mobile;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // 平台根证书一键引导（自签名 HTTPS 部署场景）
        registerPlugin(CaInstallerPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
