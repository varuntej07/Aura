/* Aura Architecture Digital Twin prototype.
   Synthetic metadata only. No API calls, browser storage, prompts, transcripts,
   audio, tool arguments, generated text, names, email addresses, or real UIDs. */
(function () {
  "use strict";

  const C = {
    client: "#60a5fa", livekit: "#5eead4", worker: "#c4b5fd",
    provider: "#fbbf24", cloudrun: "#fb7185", store: "#4ade80", muted: "#737373",
  };

  const nodes = [
    ["client.mobile", "Flutter voice client", "client", -8, 1, 0, "lib/data/services/voice_session_service.dart", "Mobile device", "verified"],
    ["api.voice_token", "GET /voice/token", "cloudrun", -6, 2.8, 0, "backend/src/main.py:213", "Cloud Run · juno-backend", "verified"],
    ["livekit.room", "LiveKit room", "livekit", -4, .8, 0, "backend/src/agent/voice_agent.py:158", "LiveKit Cloud", "verified"],
    ["worker.entrypoint", "Voice worker", "worker", -2, 2.7, 0, "backend/src/agent/voice_agent.py:158", "LiveKit Cloud Agents", "verified"],
    ["voice.endpointing", "VAD + turn detector", "worker", 0, 1.1, -1.4, "backend/src/agent/voice/pipelines.py:102", "LiveKit Cloud Agents", "verified"],
    ["voice.stt.deepgram", "Deepgram STT", "provider", 2, 2.8, -1.5, "backend/src/agent/voice/pipelines.py:24", "External provider", "verified"],
    ["voice.context", "Session context", "worker", 0, 4.2, .6, "backend/src/agent/voice/context.py:69", "LiveKit Cloud Agents", "verified"],
    ["store.firestore", "Firestore", "store", 2.2, 5.1, .7, "backend/src/agent/voice/fetchers.py:20", "Google Cloud · multi-region", "verified"],
    ["voice.llm", "LLM fallback chain", "provider", 3.8, 2.3, -.3, "backend/src/agent/voice/pipelines.py:36", "External providers", "verified"],
    ["voice.buddy", "BuddyAgent", "worker", 1.8, .2, .4, "backend/src/agent/buddy_agent.py:70", "LiveKit Cloud Agents", "verified"],
    ["api.mcp", "MCP /mcp", "cloudrun", 5.4, .7, 1.2, "backend/src/handlers/mcp.py:74", "Cloud Run · juno-backend", "verified"],
    ["tool.executor", "ToolExecutor", "cloudrun", 7.4, 2.2, .7, "backend/src/services/tool_executor.py:207", "Cloud Run · juno-backend", "verified"],
    ["voice.tts", "TTS fallback chain", "provider", 4.6, -1.2, -1.1, "backend/src/agent/voice/pipelines.py:64", "External providers", "verified"],
    ["livekit.audio", "Audio publication", "livekit", 1.3, -2.5, 0, "backend/src/agent/voice_agent.py:374", "LiveKit Cloud", "verified"],
    ["voice.recorder", "Session recorder", "worker", 3.4, 4.7, -1, "backend/src/agent/voice/recorder.py:35", "LiveKit Cloud Agents", "verified"],
    ["voice.post_session", "Post-session pipeline", "cloudrun", 6.2, 5, -.5, "backend/src/services/voice_session_summarizer.py:340", "LiveKit worker process", "verified"],
  ].map(([id,name,type,x,y,z,source,boundary,evidence]) => ({id,name,type,x,y,z,source,boundary,evidence}));

  const edges = [
    ["client.mobile","api.voice_token","token"], ["client.mobile","livekit.room","WebRTC"],
    ["livekit.room","worker.entrypoint","dispatch"], ["worker.entrypoint","voice.context","async reads"],
    ["voice.context","store.firestore","read"], ["livekit.room","voice.endpointing","audio"],
    ["voice.endpointing","voice.stt.deepgram","stream"], ["voice.stt.deepgram","voice.buddy","text metadata"],
    ["voice.buddy","voice.llm","generation"], ["voice.llm","api.mcp","tool call"],
    ["api.mcp","tool.executor","execute"], ["tool.executor","store.firestore","read/write"],
    ["voice.llm","voice.tts","text stream"], ["voice.tts","livekit.audio","PCM"],
    ["livekit.audio","client.mobile","WebRTC"], ["voice.buddy","voice.recorder","events"],
    ["voice.recorder","voice.post_session","async"], ["voice.post_session","store.firestore","write"],
  ].map(([a,b,type]) => ({a,b,type,evidence:"verified"}));

  const paths = {
    normal: ["livekit.room","voice.endpointing","voice.stt.deepgram","voice.buddy","voice.llm","voice.tts","livekit.audio","client.mobile"],
    fallback: ["livekit.room","voice.endpointing","voice.stt.deepgram","voice.buddy","voice.llm","voice.tts","livekit.audio","client.mobile"],
    tool: ["livekit.room","voice.endpointing","voice.stt.deepgram","voice.buddy","voice.llm","api.mcp","tool.executor","store.firestore","tool.executor","api.mcp","voice.llm","voice.tts","livekit.audio","client.mobile"],
  };

  const traces = [
    {id:"tr_01",label:"Session 01 · normal",kind:"normal",color:"#5eead4",duration:2380,status:"ok",tokens:612,cost:.0048,cluster:false},
    {id:"tr_02",label:"Session 02 · LLM fallback",kind:"fallback",color:"#fbbf24",duration:4210,status:"fallback",tokens:844,cost:.0087,cluster:false},
    {id:"tr_03",label:"Session 03 · slow tool",kind:"tool",color:"#fb7185",duration:6910,status:"slow",tokens:1108,cost:.0124,cluster:false},
    {id:"tr_04",label:"Session 04 · normal",kind:"normal",color:"#60a5fa",duration:2610,status:"ok",tokens:574,cost:.0042,cluster:true},
    {id:"tr_05",label:"Session 05 · normal",kind:"normal",color:"#c4b5fd",duration:2490,status:"ok",tokens:603,cost:.0045,cluster:true},
  ];

  const waterfall = {
    normal:[["Endpointing",0,260],["STT final",220,390],["LLM first token",590,790],["TTS first byte",1320,330],["Audio delivery",1670,710]],
    fallback:[["Endpointing",0,280],["STT final",240,430],["LLM primary fail",620,980],["Gemini fallback",1540,1280],["TTS first byte",2800,390],["Audio delivery",3170,1040]],
    tool:[["Endpointing",0,270],["STT final",230,410],["LLM tool call",600,720],["MCP network",1280,160],["Tool execution",1400,3760],["LLM resume",5120,710],["TTS + audio",5790,1120]],
  };

  const byId = id => nodes.find(n => n.id === id);
  let active = null;

  function mount(root) {
    if (active) active.destroy();
    root.innerHTML = `
      <section class="twin-shell" aria-label="Aura architecture digital twin prototype">
        <div class="twin-hero"><div><div class="twin-kicker">Architecture intelligence · synthetic prototype</div>
          <h1>Aura voice, alive.</h1><p>One stable topology for static mechanics, concurrent runtime paths, exact trace inspection, and evidence-gated architecture decisions.</p></div>
          <div class="twin-live"><i></i>SYNTHETIC REPLAY</div></div>
        <div class="twin-toolbar">
          <div class="seg" role="group" aria-label="Scene mode"><button class="active" data-mode="architecture">Architecture</button><button data-mode="runtime">Live Runtime</button></div>
          <button data-action="play">Pause</button><button data-action="step">Step</button>
          <label class="twin-switch">speed <select data-speed><option>.5×</option><option selected>1×</option><option>2×</option><option>4×</option></select></label>
          <input data-scrub type="range" min="0" max="1000" value="0" aria-label="Replay position"><span class="twin-time">0.0s</span>
          <label class="twin-switch"><input data-redis type="checkbox"> Hypothetical Redis</label>
          <input class="twin-search" data-search type="search" placeholder="Find component or trace" aria-label="Search components and traces">
        </div>
        <div class="twin-grid"><div class="twin-stage"><canvas data-gl aria-label="Interactive architecture scene"></canvas><canvas data-overlay aria-hidden="true"></canvas>
          <div class="twin-boundary-label" style="left:3%;top:5%">Mobile trust boundary</div><div class="twin-boundary-label" style="left:34%;top:5%">LiveKit Cloud</div><div class="twin-boundary-label" style="left:70%;top:5%">Google Cloud + providers</div>
          <div class="twin-legend"><span><i style="background:${C.livekit}"></i>runtime</span><span><i style="background:${C.provider}"></i>provider</span><span><i style="background:${C.cloudrun}"></i>Cloud Run</span><span>solid verified · dashed inferred</span></div>
        </div><aside class="twin-inspector" data-inspector aria-live="polite"></aside></div>
      </section>`;

    const shell = root.querySelector(".twin-shell");
    const base = shell.querySelector("[data-gl]");
    const overlay = shell.querySelector("[data-overlay]");
    const inspector = shell.querySelector("[data-inspector]");
    const state = {mode:"architecture",playing:true,speed:1,t:0,redis:false,selected:traces[0],yaw:-.08,pitch:.18,zoom:1,last:performance.now(),drag:null,webgl:true};
    const gl = base.getContext("webgl", {antialias:true,alpha:true});
    if (!gl) { state.webgl=false; shell.querySelector(".twin-stage").insertAdjacentHTML("beforeend",'<div class="twin-degraded">2D fallback · WebGL unavailable</div>'); }
    const ctx = overlay.getContext("2d");
    let program = null, posBuffer = null, colorBuffer = null;

    if (gl) {
      const shader=(type,src)=>{const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);return s;};
      program=gl.createProgram();
      gl.attachShader(program,shader(gl.VERTEX_SHADER,"attribute vec2 p;attribute vec3 c;varying vec3 v;uniform float size;void main(){gl_Position=vec4(p,0.,1.);gl_PointSize=size;v=c;}"));
      gl.attachShader(program,shader(gl.FRAGMENT_SHADER,"precision mediump float;varying vec3 v;uniform float roundPoint;void main(){if(roundPoint>.5&&distance(gl_PointCoord,vec2(.5))>.5)discard;gl_FragColor=vec4(v,1.);}"));
      gl.linkProgram(program); posBuffer=gl.createBuffer(); colorBuffer=gl.createBuffer();
    }

    function hex(h){return [parseInt(h.slice(1,3),16)/255,parseInt(h.slice(3,5),16)/255,parseInt(h.slice(5),16)/255];}
    function project(n,w,h){
      const cy=Math.cos(state.yaw),sy=Math.sin(state.yaw),cp=Math.cos(state.pitch),sp=Math.sin(state.pitch);
      let x=n.x*cy-n.z*sy, z=n.x*sy+n.z*cy, y=n.y*cp-z*sp; z=n.y*sp+z*cp;
      const scale=Math.min(w/19,h/10)*state.zoom*(1+z*.025); return {x:w/2+x*scale,y:h*.64-y*scale,z, nx:(w/2+x*scale)/w*2-1,ny:1-(h*.64-y*scale)/h*2};
    }
    function lineVerts(w,h){const out=[],cols=[]; edges.forEach(e=>{const a=project(byId(e.a),w,h),b=project(byId(e.b),w,h);out.push(a.nx,a.ny,b.nx,b.ny);const c=hex("#343434");cols.push(...c,...c);});return [out,cols];}
    function drawGl(w,h){
      if(!gl)return; gl.viewport(0,0,base.width,base.height);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);gl.useProgram(program);
      const pLoc=gl.getAttribLocation(program,"p"),cLoc=gl.getAttribLocation(program,"c");
      const draw=(verts,colors,mode,size,round)=>{gl.bindBuffer(gl.ARRAY_BUFFER,posBuffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(verts),gl.DYNAMIC_DRAW);gl.enableVertexAttribArray(pLoc);gl.vertexAttribPointer(pLoc,2,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,colorBuffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(colors),gl.DYNAMIC_DRAW);gl.enableVertexAttribArray(cLoc);gl.vertexAttribPointer(cLoc,3,gl.FLOAT,false,0,0);gl.uniform1f(gl.getUniformLocation(program,"size"),size*devicePixelRatio);gl.uniform1f(gl.getUniformLocation(program,"roundPoint"),round);gl.drawArrays(mode,0,verts.length/2);};
      let [v,c]=lineVerts(w,h);draw(v,c,gl.LINES,1,0);
      v=[];c=[];nodes.forEach(n=>{const p=project(n,w,h);v.push(p.nx,p.ny);c.push(...hex(C[n.type]||C.muted));});draw(v,c,gl.POINTS,11,1);
    }
    function pulsePosition(trace){const path=paths[trace.kind],p=(state.t%(trace.duration+900))/trace.duration*(path.length-1),i=Math.max(0,Math.min(path.length-2,Math.floor(p))),f=p-i,a=byId(path[i]),b=byId(path[i+1]);return {x:a.x+(b.x-a.x)*f,y:a.y+(b.y-a.y)*f,z:a.z+(b.z-a.z)*f};}
    function drawOverlay(w,h){
      ctx.clearRect(0,0,w,h);ctx.save();ctx.font="700 10px ui-monospace,Consolas,monospace";ctx.textAlign="center";
      if(!state.webgl){
        ctx.strokeStyle="#343434";ctx.lineWidth=1;
        edges.forEach(e=>{const a=project(byId(e.a),w,h),b=project(byId(e.b),w,h);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();});
        nodes.forEach(n=>{const p=project(n,w,h);ctx.fillStyle=C[n.type]||C.muted;ctx.beginPath();ctx.arc(p.x,p.y,6,0,Math.PI*2);ctx.fill();});
      }
      nodes.slice().sort((a,b)=>project(a,w,h).z-project(b,w,h).z).forEach(n=>{const p=project(n,w,h);ctx.fillStyle="rgba(0,0,0,.76)";const tw=ctx.measureText(n.name).width+12;ctx.fillRect(p.x-tw/2,p.y+9,tw,17);ctx.fillStyle=n===state.selected?"#fff":"#bdbdbd";ctx.fillText(n.name,p.x,p.y+21);});
      if(state.redis){const src=project(byId("voice.context"),w,h),dst=project(byId("store.firestore"),w,h),p={x:(src.x+dst.x)/2,y:(src.y+dst.y)/2};ctx.setLineDash([4,4]);ctx.strokeStyle=C.worker;ctx.strokeRect(p.x-44,p.y-16,88,32);ctx.setLineDash([]);ctx.fillStyle=C.worker;ctx.fillText("REDIS? overlay",p.x,p.y+4);}
      if(state.mode==="runtime"){
        const visible=traces.filter(t=>!t.cluster);visible.forEach(t=>{const p=project(pulsePosition(t),w,h);ctx.beginPath();ctx.arc(p.x,p.y,6,0,Math.PI*2);ctx.shadowBlur=18;ctx.shadowColor=t.color;ctx.fillStyle=t.color;ctx.fill();ctx.shadowBlur=0;});
        const clustered=traces.filter(t=>t.cluster);if(clustered.length){const p=project(byId("voice.llm"),w,h);ctx.fillStyle="#c4b5fd";ctx.beginPath();ctx.arc(p.x+22,p.y-18,11,0,Math.PI*2);ctx.fill();ctx.fillStyle="#100d18";ctx.font="800 9px ui-monospace";ctx.fillText("+"+clustered.length,p.x+22,p.y-15);}
      }
      ctx.restore();
    }
    function resize(){const r=base.parentElement.getBoundingClientRect(),d=devicePixelRatio||1;[base,overlay].forEach(c=>{c.style.position="absolute";c.style.inset="0";const width=Math.max(1,Math.round(r.width*d)),height=Math.max(1,Math.round(r.height*d));if(c.width!==width)c.width=width;if(c.height!==height)c.height=height;c.style.width=r.width+"px";c.style.height=r.height+"px";});ctx.setTransform(d,0,0,d,0,0);return r;}
    function traceInspector(t){const rows=waterfall[t.kind],max=t.duration;return `
      <div class="twin-kicker">Runtime trace · synthetic</div><h2>${t.label}</h2><div class="twin-sub">trace_id ${t.id} · turn turn_01 · us-central1 · metadata only</div>
      <div class="twin-badges"><span class="twin-badge synthetic">synthetic</span><span class="twin-badge">${t.status}</span>${t.cluster?'<span class="twin-badge">visually clustered</span>':''}</div>
      <div class="twin-metrics"><div class="twin-metric"><b>1.84s</b><small>p50</small></div><div class="twin-metric"><b>3.92s</b><small>p95</small></div><div class="twin-metric"><b>6.48s</b><small>p99</small></div><div class="twin-metric"><b>${t.tokens}</b><small>tokens</small></div><div class="twin-metric"><b>$${t.cost.toFixed(4)}</b><small>est. cost</small></div><div class="twin-metric"><b>${t.duration}ms</b><small>turn</small></div></div>
      <h3>Concurrent traces</h3><div class="twin-traces">${traces.map(x=>`<button class="twin-trace ${x===t?'active':''}" data-trace="${x.id}"><span class="dot" style="background:${x.color}"></span><b>${x.label}</b><small>${x.duration}ms</small></button>`).join("")}</div>
      <h3>Waterfall</h3><div class="twin-waterfall">${rows.map((r,i)=>`<div class="twin-water-row"><span>${r[0]}</span><span class="twin-water-track"><i class="twin-water-bar" style="left:${r[1]/max*100}%;width:${r[2]/max*100}%;background:${i===4&&t.kind==='tool'?C.cloudrun:t.color}"></i></span><em>${r[2]}ms</em></div>`).join("")}</div>
      <h3>Cost + fallback</h3><div class="twin-sub">input ${Math.round(t.tokens*.72)} · output ${Math.round(t.tokens*.2)} · cached ${Math.round(t.tokens*.08)} · reasoning 0<br>${t.kind==='fallback'?'OpenAI attempt failed after 980ms → Gemini Flash succeeded. Tail impact +1.54s.':t.kind==='tool'?'Tool execution owns 54% of this turn. No retry observed.':'Primary providers succeeded. No retry or fallback.'}</div>
      <h3>Advisor</h3><div class="twin-advice"><strong>${t.kind==='tool'?'Investigate tool timeout, not infrastructure':'Do nothing yet'}</strong><br>${t.kind==='tool'?'Evidence: one synthetic tool span is 3.76s. Missing: production queue delay and provider split. Add spans before considering a queue.':'No measured cache, queue, or replica bottleneck. Collect 24h of metadata-only spans before changing infrastructure.'}</div>`;}
    function nodeInspector(n){return `<div class="twin-kicker">${n.type} · ${n.evidence}</div><h2>${n.name}</h2><div class="twin-sub">${n.id}<br>${n.boundary}</div><div class="twin-badges"><span class="twin-badge verified">verified in source</span><span class="twin-badge">metadata only</span></div><div class="twin-metrics"><div class="twin-metric"><b>142ms</b><small>p50</small></div><div class="twin-metric"><b>610ms</b><small>p95</small></div><div class="twin-metric"><b>1.2s</b><small>p99</small></div></div><h3>Source</h3><div class="twin-source">${n.source}</div><h3>Dependencies</h3><div class="twin-sub">${edges.filter(e=>e.a===n.id||e.b===n.id).map(e=>`${e.a===n.id?'→ '+byId(e.b).name:'← '+byId(e.a).name} · ${e.type}`).join('<br>')||'none'}</div><h3>Telemetry</h3><div class="twin-sub">Existing: synthetic latency, status, region, provider/model.<br>Missing: production span identity, queue delay, network timing, cold-start signal.</div><h3>Privacy</h3><div class="twin-advice"><strong>Restricted operational metadata.</strong><br>Never attach prompts, transcripts, audio, generated text, tool arguments, names, email addresses, or document paths containing user content.</div>`;}
    function bindInspector(){inspector.querySelectorAll("[data-trace]").forEach(b=>b.onclick=()=>{state.selected=traces.find(t=>t.id===b.dataset.trace);renderInspector();});}
    function renderInspector(){inspector.innerHTML=state.selected.kind?traceInspector(state.selected):nodeInspector(state.selected);bindInspector();}
    function tick(now){if(!shell.isConnected)return;if(state.playing&&state.mode==="runtime")state.t=(state.t+(now-state.last)*state.speed)%7800;state.last=now;const r=resize();drawGl(r.width,r.height);drawOverlay(r.width,r.height);shell.querySelector("[data-scrub]").value=Math.round(state.t/7800*1000);shell.querySelector(".twin-time").textContent=(state.t/1000).toFixed(1)+"s";requestAnimationFrame(tick);}

    shell.querySelectorAll("[data-mode]").forEach(b=>b.onclick=()=>{state.mode=b.dataset.mode;shell.querySelectorAll("[data-mode]").forEach(x=>x.classList.toggle("active",x===b));});
    shell.querySelector("[data-action=play]").onclick=e=>{state.playing=!state.playing;e.target.textContent=state.playing?"Pause":"Play";};
    shell.querySelector("[data-action=step]").onclick=()=>{state.playing=false;state.t=(state.t+250)%7800;shell.querySelector("[data-action=play]").textContent="Play";};
    shell.querySelector("[data-speed]").onchange=e=>state.speed=parseFloat(e.target.value);
    shell.querySelector("[data-scrub]").oninput=e=>{state.t=Number(e.target.value)/1000*7800;state.playing=false;shell.querySelector("[data-action=play]").textContent="Play";};
    shell.querySelector("[data-redis]").onchange=e=>{state.redis=e.target.checked;if(state.redis)inspector.innerHTML='<div class="twin-kicker">Hypothetical overlay</div><h2>Redis context cache</h2><div class="twin-badges"><span class="twin-badge twin-redis">estimate, not observed</span></div><div class="twin-advice"><strong>Placement</strong><br>Between Session context and Firestore for profile, memory digest, latest-session, archive, Aura, and entitlement reads.<br><br><strong>Estimated effect</strong><br>Warm-hit pre-session context: 420ms → 90ms. Firestore reads: -55% for repeat callers. Added cost: ~$35/month baseline.<br><br><strong>Assumptions</strong><br>60% repeat-session hit rate, 5 minute TTL, no measured production redundancy yet. Consistency risk: stale profile or entitlement. Recommendation: do nothing until repeated-read spans exceed 25% of p95 or Firestore read cost crosses the agreed budget.</div>';};
    shell.querySelector("[data-search]").oninput=e=>{const q=e.target.value.toLowerCase().trim(),n=nodes.find(x=>(x.name+x.id+x.source).toLowerCase().includes(q)),t=traces.find(x=>x.label.toLowerCase().includes(q));if(q&&(n||t)){state.selected=n||t;renderInspector();}};
    overlay.onpointerdown=e=>{state.drag={x:e.clientX,y:e.clientY,moved:false};overlay.setPointerCapture(e.pointerId);};
    overlay.onpointermove=e=>{if(!state.drag)return;const dx=e.clientX-state.drag.x,dy=e.clientY-state.drag.y;if(Math.abs(dx)+Math.abs(dy)>2)state.drag.moved=true;state.yaw+=dx*.006;state.pitch=Math.max(-.5,Math.min(.6,state.pitch+dy*.004));state.drag.x=e.clientX;state.drag.y=e.clientY;};
    overlay.onpointerup=e=>{if(state.drag&&!state.drag.moved){const r=overlay.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,n=nodes.map(n=>[n,project(n,r.width,r.height)]).sort((a,b)=>(a[1].x-x)**2+(a[1].y-y)**2-((b[1].x-x)**2+(b[1].y-y)**2))[0];if(n&&Math.hypot(n[1].x-x,n[1].y-y)<34){state.selected=n[0];renderInspector();}}state.drag=null;};
    overlay.onwheel=e=>{e.preventDefault();state.zoom=Math.max(.65,Math.min(1.8,state.zoom*(e.deltaY>0?.92:1.08)));};
    renderInspector();requestAnimationFrame(tick);
    active={destroy(){active=null;}};
  }

  window.AuraArchitectureTwin = {mount};
})();
