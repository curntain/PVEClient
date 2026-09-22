package com.pveclient.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
import android.webkit.WebChromeClient;
import android.webkit.SslErrorHandler;
import android.net.http.SslError;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

public class MainActivity extends Activity {
    private static final String PREFS = "pveclient";
    private static final String KEY_URL = "server_url";

    private WebView webView;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowContentAccess(true);
        settings.setAllowFileAccess(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) { handler.cancel(); }
        });
        webView.setWebChromeClient(new WebChromeClient());

        String url = prefs.getString(KEY_URL, "");
        if (url == null || url.trim().isEmpty() || !validUrl(url)) {
            askForServerUrl();
        } else {
            loadUrl(url);
        }
    }

    private void askForServerUrl() {
        final EditText input = new EditText(this);
        input.setHint("https://your-pveclient.example.com/");
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);

        new AlertDialog.Builder(this)
                .setTitle("PVEClient 服务器地址")
                .setMessage("请输入可以从手机访问的 PVEClient Web 地址。")
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("打开", (dialog, which) -> {
                    String url = input.getText().toString().trim();
                    if (!url.isEmpty() && validUrl(url)) {
                        prefs.edit().putString(KEY_URL, url).apply();
                        loadUrl(url);
                    } else {
                        input.setError("请输入 HTTPS 地址；HTTP 仅允许 localhost 或内网地址");
                        askForServerUrl();
                    }
                })
                .show();
    }

    private boolean validUrl(String value) {
        String url = value.contains("://") ? value : "https://" + value;
        android.net.Uri uri = android.net.Uri.parse(url);
        String scheme = uri.getScheme();
        String host = uri.getHost();
        if (host == null || !("https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme))) return false;
        if ("https".equalsIgnoreCase(scheme)) return true;
        if ("localhost".equalsIgnoreCase(host) || host.endsWith(".local")) return true;
        String[] octets = host.split("\\.");
        try {
            if (octets.length != 4) return false;
            int a = Integer.parseInt(octets[0]), b = Integer.parseInt(octets[1]);
            return a == 10 || a == 127 || (a == 192 && b == 168) || (a == 172 && b >= 16 && b <= 31);
        } catch (NumberFormatException ex) { return false; }
    }

    private void loadUrl(String value) {
        String url = value.contains("://") ? value : "https://" + value;
        webView.loadUrl(url);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
