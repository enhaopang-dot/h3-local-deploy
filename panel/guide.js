/* H3 影像工坊 · 使用手册抽屉：自带样式与 DOM，不改动主界面逻辑 */
(function () {
  'use strict';

  var CSS = `
  #guide-btn { margin-left:auto; background:#1b293d; color:var(--ink); border:1px solid var(--line);
    border-radius:999px; padding:5px 14px; font:inherit; font-size:13px; cursor:pointer; white-space:nowrap; }
  #guide-btn:hover { border-color:var(--green); color:var(--green); }
  #guide-drawer { position:fixed; top:0; right:0; bottom:0; width:460px; max-width:94vw;
    background:var(--card); border-left:1px solid var(--line); box-shadow:-8px 0 26px rgba(2,8,16,.5);
    z-index:9999; display:none; flex-direction:column; }
  #guide-drawer.open { display:flex; }
  #guide-head { display:flex; align-items:center; justify-content:space-between; padding:14px 18px;
    border-bottom:1px solid var(--line); background:var(--nav); flex:none; }
  #guide-head b { font-size:15px; }
  #guide-head small { color:var(--weak); font-size:12px; margin-left:8px; }
  #guide-close { background:none; border:none; color:var(--sub); font-size:22px; cursor:pointer; padding:0 4px; }
  #guide-close:hover { color:var(--red); }
  #guide-body { overflow-y:auto; padding:6px 18px 40px; font-size:13.5px; line-height:1.62; }
  #guide-body details { border-bottom:1px solid var(--line); padding:10px 0; }
  #guide-body summary { cursor:pointer; font-weight:600; color:var(--green); font-size:14px; list-style:none; padding:4px 0; }
  #guide-body summary::-webkit-details-marker { display:none; }
  #guide-body summary::before { content:"▸ "; color:var(--weak); }
  #guide-body details[open] summary::before { content:"▾ "; }
  #guide-body p { margin:6px 0; color:var(--sub); }
  #guide-body ul { margin:6px 0 6px 4px; padding-left:16px; color:var(--sub); }
  #guide-body li { margin:3px 0; }
  #guide-body b { color:var(--ink); }
  #guide-body code { background:var(--input); border:1px solid var(--line); border-radius:5px;
    padding:1px 5px; font-size:12.5px; color:var(--amber); }
  #guide-body table { width:100%; border-collapse:collapse; margin:8px 0; font-size:12.5px; }
  #guide-body th, #guide-body td { border:1px solid var(--line); padding:5px 7px; text-align:left; vertical-align:top; }
  #guide-body th { background:var(--input); color:var(--ink); font-weight:600; }
  #guide-body td { color:var(--sub); }
  #guide-body .tip { background:var(--green-bg); border-left:3px solid var(--green); padding:7px 10px;
    border-radius:4px; margin:8px 0; color:var(--ink); }
  #guide-body .warn { background:rgba(244,141,127,.10); border-left:3px solid var(--red); padding:7px 10px;
    border-radius:4px; margin:8px 0; color:var(--ink); }
  `;

  var HTML = `
  <div id="guide-head">
    <span><b>📖 使用手册</b><small>哪个页签干什么、参数怎么填</small></span>
    <button id="guide-close" title="关闭 (Esc)">×</button>
  </div>
  <div id="guide-body">

    <details open>
      <summary>十一个页签，各管什么</summary>
      <table>
        <tr><th>页签</th><th>什么时候用</th></tr>
        <tr><td>✍️ 文字生图</td><td>先用它把画面试出来（走 Z-Image-Turbo，快）</td></tr>
        <tr><td>🎬 文字生视频</td><td>只有想法、没有图</td></tr>
        <tr><td>🖼️ 图片生视频</td><td><b>最常用</b>：手里有图，让它动起来</td></tr>
        <tr><td>👤 参考生视频</td><td>要角色/画风跟参考图一致</td></tr>
        <tr><td>⏩ 视频延续</td><td>把已有片段往后接，超 15 秒自动拆段</td></tr>
        <tr><td>🎭 视频改视频</td><td>保留动作结构，换风格/换内容</td></tr>
        <tr><td>🔍 视频超分</td><td>片子糊了放大，比重新生成快得多（0.1~0.3 秒/帧）</td></tr>
        <tr><td>🎞️ 视频截帧</td><td>从成片里抠首帧/尾帧，接着再用</td></tr>
        <tr><td>🖌️ 图改图 / 🪄 AI改图</td><td>改图；AI改图是"说人话改图"，不用写提示词</td></tr>
        <tr><td>✨ 照片清晰化</td><td>人像修复放大</td></tr>
      </table>
      <div class="tip">口诀：<b>先有图再动</b>。绝大多数活是「生图工具出图 → 这里图片生视频」，纯文字生视频反而少见。</div>
    </details>

    <details>
      <summary>质量档位与时长怎么填</summary>
      <ul>
        <li><b>草稿档</b>：8 步，约 7 分钟一条。用来试构图和运镜，别用它出成品</li>
        <li><b>精细档</b>：更慢，质量更好。定稿才用</li>
        <li><b>时长</b>：秒数可以自己填，<b>超过 15 秒自动拆段连跑</b>再拼起来——所以长片不是一镜到底，中间可能有接缝</li>
        <li><b>首次加载</b>：约 2~3 分钟，属正常，别以为卡死</li>
      </ul>
    </details>

    <details>
      <summary>参考生视频：怎么让角色不跑偏</summary>
      <ul>
        <li>主图框放 1 张（这张是"身份基准"）</li>
        <li>辅助参考框最多再放 3 张（换角度、换场景的同一个人）</li>
        <li>参考图越"干净"越好：单人、正面、光线均匀；多人合影或背影效果差</li>
        <li>提示词里描述<b>动作和镜头</b>，不要重复描述长相（长相由参考图管）</li>
      </ul>
    </details>

    <details>
      <summary>AI 优化提示词：快速档 vs 精细档</summary>
      <ul>
        <li><b>快速档</b>：qwen2.5vl 7B，看图够用，40~90 秒</li>
        <li><b>精细档</b>：qwen3-vl，更懂画面，慢一些</li>
        <li>生成前程序会自动把 Ollama 模型从显存卸掉，别手动再开一个，会抢显存</li>
      </ul>
    </details>

    <details>
      <summary>和生图工具怎么配合</summary>
      <ul>
        <li><b>图从生图工具来</b>：先在别的工具出图/改图，再到这里「图片生视频」上传</li>
        <li><b>成片位置</b>：在 <code>输出目录的 video/</code>，可直接拖进剪辑软件</li>
        <li>要图里带准确文字（主图、海报）→ 生图工具的 <b>带文字排版能力的生图模型</b>，这里做不了</li>
      </ul>
    </details>

    <details>
      <summary>常见坑</summary>
      <ul>
        <li><b>ComfyUI 没开</b>：双击 <code>scripts/03_start_comfyui.sh</code>，等 8188 起来</li>
        <li><b>显存被占</b>：ComfyUI 里别同时挂别的任务；生成前先让 Ollama 模型卸载</li>
        <li><b>中文路径</b>：面板脚本启动路径由 PowerShell 处理，别自己改回 bat 里写中文</li>
        <li><b>报 Tensor 越界</b>：模型文件下载断过，用 <code>check_safetensors.py</code> 扫一遍</li>
      </ul>
    </details>

  </div>
  `;

  function mount() {
    if (document.getElementById('guide-drawer')) return;

    var style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    var drawer = document.createElement('div');
    drawer.id = 'guide-drawer';
    drawer.innerHTML = HTML;
    document.body.appendChild(drawer);

    var topbar = document.querySelector('.topbar');
    var btn = document.createElement('button');
    btn.id = 'guide-btn';
    btn.textContent = '📖 使用手册';
    btn.title = '哪个页签干什么、参数怎么填 (G)';
    if (topbar) topbar.appendChild(btn);

    function toggle() { drawer.classList.toggle('open'); }
    btn.addEventListener('click', toggle);
    drawer.querySelector('#guide-close').addEventListener('click', function () { drawer.classList.remove('open'); });
    document.addEventListener('keydown', function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || '';
      if (e.key === 'Escape') drawer.classList.remove('open');
      if ((e.key === 'g' || e.key === 'G') && !/INPUT|TEXTAREA|SELECT/.test(tag)) toggle();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
