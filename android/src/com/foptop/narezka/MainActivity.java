package com.foptop.narezka;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.View;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * «Нарезка» as an Android app: the editor page runs in a WebView served from the APK's assets
 * at https://appassets.androidplatform.net/ (a secure origin, so storage, microphone and workers work).
 * The JS bridge adds what a browser page can't do: save the exported video to the gallery and
 * call the Fish Audio API without browser CORS limits.
 */
public class MainActivity extends Activity {
    static final String HOST = "appassets.androidplatform.net";
    static final String START = "https://" + HOST + "/index.html";
    static final int REQ_FILES = 11, REQ_MIC = 12, REQ_STORAGE = 13;

    WebView web;
    ValueCallback<Uri[]> fileCb;
    PermissionRequest pendingPerm;
    final ExecutorService pool = Executors.newCachedThreadPool();
    final HashMap<String, OutputStream> saves = new HashMap<>();
    final HashMap<String, Uri> saveUris = new HashMap<>();
    final HashMap<String, File> saveFiles = new HashMap<>();

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        Window w = getWindow();
        w.setStatusBarColor(Color.rgb(0x11, 0x12, 0x14));
        w.setNavigationBarColor(Color.rgb(0x18, 0x19, 0x1c));

        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(0x11, 0x12, 0x14));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setTextZoom(100);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.addJavascriptInterface(new Bridge(), "NarezkaNative");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (!HOST.equals(u.getHost())) return null;
                String path = u.getPath();
                if (path == null || path.equals("/") || path.isEmpty()) path = "/index.html";
                Map<String, String> h = new HashMap<>();
                h.put("Access-Control-Allow-Origin", "*");
                h.put("Cache-Control", "no-cache");
                try {
                    InputStream in = getAssets().open("www" + path);
                    return new WebResourceResponse(mime(path), mime(path).startsWith("text/") ? "utf-8" : null, 200, "OK", h, in);
                } catch (IOException e) {
                    return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", h, new ByteArrayInputStream(new byte[0]));
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (HOST.equals(u.getHost())) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (ActivityNotFoundException e) { /* nothing to open it */ }
                return true;
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                ArrayList<String> types = new ArrayList<>();
                String[] acc = p.getAcceptTypes();
                if (acc != null) for (String a : acc) if (a != null) for (String t : a.split(",")) {
                    t = t.trim();
                    if (t.contains("/")) types.add(t);
                }
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                if (types.size() == 1) i.setType(types.get(0));
                else {
                    i.setType("*/*");
                    if (types.size() > 1) i.putExtra(Intent.EXTRA_MIME_TYPES, types.toArray(new String[0]));
                }
                if (p.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try { startActivityForResult(i, REQ_FILES); }
                catch (ActivityNotFoundException e) { fileCb = null; cb.onReceiveValue(null); return false; }
                return true;
            }

            @Override
            public void onPermissionRequest(PermissionRequest req) {
                boolean audio = false;
                for (String r : req.getResources()) if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) audio = true;
                if (!audio) { req.deny(); return; }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                    req.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                } else {
                    if (pendingPerm != null) pendingPerm.deny();
                    pendingPerm = req;
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
                }
            }
        });

        setContentView(web);
        if (b != null) web.restoreState(b);
        else web.loadUrl(START);
    }

    static String mime(String path) {
        String p = path.toLowerCase();
        if (p.endsWith(".html")) return "text/html";
        if (p.endsWith(".js") || p.endsWith(".mjs")) return "text/javascript";
        if (p.endsWith(".css")) return "text/css";
        if (p.endsWith(".json")) return "application/json";
        if (p.endsWith(".wasm")) return "application/wasm";
        if (p.endsWith(".png")) return "image/png";
        if (p.endsWith(".mp3")) return "audio/mpeg";
        if (p.endsWith(".svg")) return "image/svg+xml";
        return "application/octet-stream";
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        if (code != REQ_FILES) { super.onActivityResult(code, result, data); return; }
        if (fileCb == null) return;
        Uri[] out = null;
        if (result == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null && clip.getItemCount() > 0) {
                out = new Uri[clip.getItemCount()];
                for (int i = 0; i < out.length; i++) out[i] = clip.getItemAt(i).getUri();
            } else if (data.getData() != null) out = new Uri[]{data.getData()};
        }
        fileCb.onReceiveValue(out);
        fileCb = null;
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        if (code == REQ_MIC && pendingPerm != null) {
            if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED)
                pendingPerm.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            else pendingPerm.deny();
            pendingPerm = null;
        }
    }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("(window.__narezkaBack ? String(window.__narezkaBack()) : 'false')", new ValueCallback<String>() {
            @Override public void onReceiveValue(String v) {
                if (v == null || !v.contains("true")) moveTaskToBack(true);
            }
        });
    }

    @Override
    protected void onDestroy() {
        for (OutputStream o : saves.values()) try { o.close(); } catch (IOException e) { /* ignore */ }
        pool.shutdownNow();
        web.destroy();
        super.onDestroy();
    }

    void reply(String id, int status, String type, String data) {
        final String js = "window.__nativeHttp&&window.__nativeHttp(" + JSONObject.quote(id) + "," + status + ","
                + JSONObject.quote(type == null ? "" : type) + "," + JSONObject.quote(data == null ? "" : data) + ")";
        web.post(new Runnable() { @Override public void run() { web.evaluateJavascript(js, null); } });
    }

    class Bridge {
        @JavascriptInterface
        public String version() { return "1"; }

        /** Network call for the Fish Audio API (only that host), answered through window.__nativeHttp. */
        @JavascriptInterface
        public void http(final String id, final String method, final String url, final String headersJson, final String body) {
            if (url == null || !url.startsWith("https://api.fish.audio/")) { reply(id, 0, null, "host not allowed"); return; }
            pool.execute(new Runnable() { @Override public void run() {
                HttpURLConnection c = null;
                try {
                    c = (HttpURLConnection) new URL(url).openConnection();
                    c.setConnectTimeout(20000);
                    c.setReadTimeout(180000);
                    c.setRequestMethod(method == null ? "GET" : method);
                    JSONObject h = new JSONObject(headersJson == null || headersJson.isEmpty() ? "{}" : headersJson);
                    Iterator<String> it = h.keys();
                    while (it.hasNext()) { String k = it.next(); c.setRequestProperty(k, h.optString(k)); }
                    if (body != null && !body.isEmpty() && !"GET".equals(method)) {
                        c.setDoOutput(true);
                        byte[] bb = body.getBytes("UTF-8");
                        c.setFixedLengthStreamingMode(bb.length);
                        OutputStream o = c.getOutputStream();
                        o.write(bb);
                        o.close();
                    }
                    int st = c.getResponseCode();
                    InputStream in = st >= 400 ? c.getErrorStream() : c.getInputStream();
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    if (in != null) {
                        byte[] buf = new byte[65536];
                        int n;
                        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
                        in.close();
                    }
                    reply(id, st, c.getContentType(), Base64.encodeToString(bo.toByteArray(), Base64.NO_WRAP));
                } catch (Exception e) {
                    reply(id, 0, null, String.valueOf(e.getMessage()));
                } finally {
                    if (c != null) c.disconnect();
                }
            } });
        }

        /** Starts saving a video to the gallery (Movies/Нарезка). Returns an id, or "" if storage permission is needed. */
        @JavascriptInterface
        public String saveStart(String name, String mime) {
            try {
                String id = Long.toString(System.nanoTime(), 36);
                if (mime == null || mime.isEmpty()) mime = "video/mp4";
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues v = new ContentValues();
                    v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                    v.put(MediaStore.MediaColumns.MIME_TYPE, mime);
                    v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_MOVIES + "/Нарезка");
                    v.put(MediaStore.MediaColumns.IS_PENDING, 1);
                    ContentResolver cr = getContentResolver();
                    Uri u = cr.insert(MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY), v);
                    if (u == null) return "";
                    saves.put(id, cr.openOutputStream(u));
                    saveUris.put(id, u);
                } else {
                    if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                        runOnUiThread(new Runnable() { @Override public void run() { requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_STORAGE); } });
                        return "";
                    }
                    File dir = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MOVIES), "Нарезка");
                    dir.mkdirs();
                    File f = new File(dir, name);
                    saves.put(id, new FileOutputStream(f));
                    saveFiles.put(id, f);
                }
                return id;
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public boolean saveChunk(String id, String b64) {
            OutputStream o = saves.get(id);
            if (o == null) return false;
            try { o.write(Base64.decode(b64, Base64.DEFAULT)); return true; }
            catch (Exception e) { return false; }
        }

        @JavascriptInterface
        public String saveEnd(String id) {
            OutputStream o = saves.remove(id);
            if (o == null) return "no such save";
            try {
                o.close();
                Uri u = saveUris.remove(id);
                if (u != null) {
                    ContentValues v = new ContentValues();
                    v.put(MediaStore.MediaColumns.IS_PENDING, 0);
                    getContentResolver().update(u, v, null, null);
                }
                File f = saveFiles.remove(id);
                if (f != null) MediaScannerConnection.scanFile(MainActivity.this, new String[]{f.getAbsolutePath()}, null, null);
                return "ok";
            } catch (Exception e) {
                return String.valueOf(e.getMessage());
            }
        }

        @JavascriptInterface
        public void saveAbort(String id) {
            OutputStream o = saves.remove(id);
            try { if (o != null) o.close(); } catch (IOException e) { /* ignore */ }
            Uri u = saveUris.remove(id);
            if (u != null) try { getContentResolver().delete(u, null, null); } catch (Exception e) { /* ignore */ }
            File f = saveFiles.remove(id);
            if (f != null) f.delete();
        }
    }
}
