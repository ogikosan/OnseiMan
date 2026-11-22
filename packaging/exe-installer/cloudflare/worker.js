// OnseiMan の配布サイト。R2 の onseiman-releases に置いたファイルを配る。
//   /              ダウンロードページ
//   /download      最新のインストーラーへ転送
//   /latest.json   アプリが更新の確認に使う最新版の情報
//   /files/OnseiMan-Setup-<版>.exe  インストーラー本体
const INSTALLER_RE = /^files\/OnseiMan-Setup-[0-9][0-9.]*\.exe$/;

async function latest(env) {
  const obj = await env.RELEASES.get("latest.json");
  if (!obj) return null;
  try { return JSON.parse(await obj.text()); } catch { return null; }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function page(info) {
  const ver = info ? esc(info.version) : "";
  const notes = info && info.notes ? `<p class="notes">${esc(info.notes)}</p>` : "";
  const button = info
    ? `<a class="dl" href="/download">ダウンロード（Windows 版 ${ver}）</a>`
    : `<p>まだ公開されていません。</p>`;
  return `<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OnseiMan</title>
<style>
:root{--bg:#f4f6fa;--card:#fff;--text:#1d2842;--muted:#5d6782;--accent:#2f6bf2;--line:#e3e7ef}
@media (prefers-color-scheme:dark){:root{--bg:#0f1420;--card:#182032;--text:#e4e9f4;--muted:#9aa5bd;--accent:#7f9bff;--line:#273249}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.8 "Segoe UI","Meiryo UI","Hiragino Sans",sans-serif}
main{max-width:720px;margin:0 auto;padding:40px 16px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.lead{color:var(--muted);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:14px}
.dl{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:8px}
.notes{color:var(--muted);font-size:14px;margin:12px 0 0;white-space:pre-wrap}
ol,ul{margin:0;padding-left:1.4em}li{margin:4px 0}
kbd{border:1px solid var(--line);border-radius:5px;padding:0 6px;font:inherit;font-size:13px;background:var(--bg)}
small{color:var(--muted)}
</style></head><body><main>
<h1>OnseiMan</h1>
<p class="lead">話した言葉を文字にして、カーソルのある場所に入力する Windows 用の音声入力アプリです。音声はインターネットに送らず、このパソコンの中だけで文字にします。</p>
<div class="card">${button}${notes}
<p><small>Windows 10 / 11（64ビット）。最初の起動時に、音声認識のデータ（約 840MB）を自動でダウンロードします。空き容量は 1.5GB ほど必要です。</small></p></div>
<h2>入れ方</h2>
<div class="card"><ol>
<li>上のボタンからダウンロードしたファイルを開きます。</li>
<li>「Windows によって PC が保護されました」と出たら、「詳細情報」→「実行」を押します（個人で作ったアプリのため表示されます）。</li>
<li>画面に従って進めると、OnseiMan が起動します。初回だけ、データのダウンロードに数分かかります。</li>
</ol></div>
<h2>使い方</h2>
<div class="card"><ol>
<li>文字を入れたい場所をクリックします。</li>
<li>右側の <kbd>Alt</kbd> キーを1回押して、話します。</li>
<li>話し終えたら、もう一度 <kbd>Alt</kbd>（または黙って2秒待つ）。文字が入ります。</li>
</ol>
<p><small>くわしい使い方は、タスクバーの OnseiMan のアイコンを右クリック →「使い方（ヘルプ）」で見られます。新しいバージョンが出ると、起動したときに更新するか聞かれます。</small></p></div>
</main></body></html>`;
}

export default {
  async fetch(req, env) {
    if (req.method !== "GET" && req.method !== "HEAD") return new Response("Method Not Allowed", { status: 405 });
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/latest.json") {
      const obj = await env.RELEASES.get("latest.json");
      if (!obj) return new Response("{}", { status: 404, headers: { "content-type": "application/json" } });
      return new Response(obj.body, {
        headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-cache" },
      });
    }

    if (path === "/download") {
      const info = await latest(env);
      if (!info || !info.url) return new Response("まだ公開されていません", { status: 404 });
      return Response.redirect(info.url, 302);
    }

    if (path.startsWith("/files/")) {
      const key = decodeURIComponent(path.slice(1));
      if (!INSTALLER_RE.test(key)) return new Response("Not Found", { status: 404 });
      const obj = await env.RELEASES.get(key);
      if (!obj) return new Response("Not Found", { status: 404 });
      return new Response(req.method === "HEAD" ? null : obj.body, {
        headers: {
          "content-type": "application/vnd.microsoft.portable-executable",
          "content-length": String(obj.size),
          "content-disposition": `attachment; filename="${key.slice(6)}"`,
          "cache-control": "public, max-age=31536000, immutable",
          etag: obj.httpEtag,
        },
      });
    }

    if (path === "/" || path === "/index.html") {
      const info = await latest(env);
      return new Response(page(info), {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" },
      });
    }
    return new Response("Not Found", { status: 404 });
  },
};
