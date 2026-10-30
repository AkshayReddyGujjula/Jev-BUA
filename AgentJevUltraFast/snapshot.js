(() => {
  if (!document.body) return null;
  const secrets = typeof privateValues === 'undefined' ? [] : privateValues;
  const mask = raw => {
    let value=String(raw ?? '');
    for (const secret of secrets) if (secret) {
      if (secret.length<4) { if (value===secret) value='[private]'; }
      else value=value.split(secret).join('[private]');
    }
    return value;
  };
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const privateCode = e => e.matches?.('input[autocomplete="one-time-code"]') ||
    /\b(otp|one[- ]?time (?:code|password)|verification code|security code|2fa|mfa)\b/i.test(
      [e.getAttribute('aria-label'),e.getAttribute('placeholder'),e.getAttribute('name'),
        e.getAttribute('id'),[...(e.labels||[])].map(l=>l.innerText).join(' ')].join(' '));
  const safe = e => !['password','file','hidden'].includes(e.type) && !privateCode(e);
  // The main video and the player around it (smallest ancestor up to 1.5x the video's area).
  const videos=[...document.querySelectorAll('video')];
  // A thumbnail-sized paused clip in an article is not the page's player.
  const video=videos.map(v=>[v,v.getBoundingClientRect()])
    .filter(([v,r])=>r.width*r.height>0 && (!v.paused || (r.width>=320 && r.height>=180)))
    .sort((a,b)=>b[1].width*b[1].height-a[1].width*a[1].height).map(([v])=>v)[0] || null;
  let player=null;
  if (video) {
    const r=video.getBoundingClientRect(), area=r.width*r.height;
    for (let p=video.parentElement; p && p!==document.body; p=p.parentElement) {
      const q=p.getBoundingClientRect(); if (q.width*q.height>area*1.5) break; player=p;
    }
  }
  cache.player=player;   // act() accepts the same auto-hidden player controls
  // Video players fade their controls to opacity 0 while playing; they stay clickable and
  // reappear on pointer movement. Only inside the player is opacity ignored: elsewhere an
  // opacity-0 element is usually a closed menu mid-transition.
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    (e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
      (!!player && player.contains(e) && e.checkVisibility({checkVisibilityCSS:true})));
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton','slider'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),mask(e.value),e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.focusKey=()=>document.activeElement ? identity(document.activeElement) : null;
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),mask(name(e)),mask(e.value??null),e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      mask(e.getAttribute('href')),mask(scope?.innerText?.slice(0,6000)||'')];
  };
  // Stable identity for replay only (never sent to a model): what a recorded step can re-find
  // the element by when its visible label drifts or repeats. Generated ids are ignored.
  const generatedId=/^(:r|radix-|headlessui-|mui-|react-aria|ember\d)|[0-9a-f]{8,}|\d{4,}/i;
  const anchor=(e)=>{
    const a={}, test=e.getAttribute('data-testid')||e.getAttribute('data-test')||
      e.getAttribute('data-qa')||e.getAttribute('data-cy');
    if (test) a.testid=mask(test);
    if (e.id && !generatedId.test(e.id)) a.id=mask(e.id);
    const field=e.getAttribute('name'); if (field) a.name=mask(field);
    const hint=e.getAttribute('placeholder'); if (hint) a.placeholder=mask(hint);
    const link=e.getAttribute('href');
    if (link && !/^(javascript|data):/i.test(link)) { try { a.href=mask(new URL(link,location.href).pathname); } catch {} }
    const box=e.closest('dialog,[role="dialog"],[role="alertdialog"],form,section,nav,aside,header,[role="region"],[role="menu"]');
    const title=box && (box.getAttribute('aria-label') ||
      box.querySelector('h1,h2,h3,h4,legend,[role="heading"]')?.innerText);
    if (title) a.container=mask(title.trim().slice(0,60));
    return a;
  };
  const actions=[];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:mask(name(e)||rname),
      form_submit:(!!e.form && e.type==='submit'),
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}, anchor:anchor(e)};
    for (const key of ['checked','selected','expanded','pressed']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (rname==='slider') {
      const min=Number(e.getAttribute('aria-valuemin') ?? 0);
      const max=Number(e.getAttribute('aria-valuemax') ?? 100);
      const now=Number(e.getAttribute('aria-valuenow') ?? min);
      actions.push({...base, kind:'seek', value:String(now), min, max,
        label:base.label+' ['+min+'-'+max+']'});
      continue;
    }
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:mask(o.value),
          current_value:mask([...e.selectedOptions].map(o=>o.label).join(', ')),label:base.label+' → '+mask(o.label)});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? mask(e.value) :
        e.isContentEditable || rname==='combobox' ? mask(e.innerText.trim()) : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(mask(value)); length+=value.length;
    }
  }
  const text=mask(words.join('\n').slice(0,6000)), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,mask(location.href),scrollX,scrollY,innerWidth,innerHeight,
    mask(document.title),text,semantics,page_key[6],
    document.activeElement ? identity(document.activeElement) : null];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  const keys=[{id:'key_escape',key:'Escape',code:'Escape',vk:27,label:'Press Escape'},
    {id:'key_enter',key:'Enter',code:'Enter',vk:13,label:'Press Enter'}];
  // Shortcuts the page itself advertises with the standard aria-keyshortcuts attribute
  // (video players, mail and document apps). Nothing site-specific is hardcoded: a key the
  // page does not declare is never offered. Each carries its element's role and name, so the
  // confidence gate treats "Press Control+Enter (page shortcut: Send)" like clicking Send.
  const NAMED={Enter:13,Escape:27,Esc:27,Tab:9,Space:32,Spacebar:32,Backspace:8,Delete:46,Home:36,
    End:35,PageUp:33,PageDown:34,ArrowLeft:37,ArrowUp:38,ArrowRight:39,ArrowDown:40};
  const PUNCT={'/':[191,'Slash'],'?':[191,'Slash'],'.':[190,'Period'],'>':[190,'Period'],
    ',':[188,'Comma'],'<':[188,'Comma'],';':[186,'Semicolon'],'=':[187,'Equal'],'-':[189,'Minus'],
    '[':[219,'BracketLeft'],']':[221,'BracketRight'],"'":[222,'Quote']};
  const BITS={alt:1,control:2,ctrl:2,meta:4,shift:8};
  const combo=spec=>{
    const parts=spec.split('+'); let key=parts.pop();
    const mods=parts.map(m=>m.toLowerCase());
    if (!key || mods.some(m=>!(m in BITS))) return null;
    const modifiers=mods.reduce((n,m)=>n|BITS[m],0);
    if (/^[a-z]$/i.test(key)) {
      key=modifiers&8 ? key.toUpperCase() : key.toLowerCase();
      return {key,code:'Key'+key.toUpperCase(),vk:key.toUpperCase().charCodeAt(0),modifiers};
    }
    if (/^[0-9]$/.test(key)) return {key,code:'Digit'+key,vk:key.charCodeAt(0),modifiers};
    if (key in PUNCT) return {key,code:PUNCT[key][1],vk:PUNCT[key][0],modifiers};
    if (key in NAMED) {
      const code=key==='Esc' ? 'Escape' : key.startsWith('Space') ? 'Space' : key;
      return {key:code==='Space' ? ' ' : code,code,vk:NAMED[key],modifiers};
    }
    return null;
  };
  const advertised=new Set(['Escape','Enter']);
  for (const e of document.querySelectorAll('[aria-keyshortcuts]')) {
    if (keys.length>=14) break;
    if (e.closest('[inert]') || !e.checkVisibility({checkVisibilityCSS:true}) ||
      e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    // A link's shortcut only navigates, and clicking the link does the same; offer controls.
    if (['link',null].includes(role(e)) && e.tagName!=='BUTTON') continue;
    const spec=(e.getAttribute('aria-keyshortcuts')||'').trim().split(/\s+/)[0], k=combo(spec);
    const what=mask(name(e)||e.getAttribute('title')||'').replace(/\s+/g,' ').trim().slice(0,80);
    if (!k || !what || advertised.has(spec)) continue;
    advertised.add(spec);
    const state=['pressed','checked','expanded'].map(a=>[a,e.getAttribute('aria-'+a)])
      .filter(([,v])=>v!==null).map(([a,v])=>a+'='+v).join(', ');
    keys.push({id:'key_'+spec.toLowerCase().replace(/[^a-z0-9]+/g,'_'),...k,shortcut:true,
      role:role(e)||'button',label:'Press '+spec+' (page shortcut: '+what+')'+(state ? ' ['+state+']' : '')});
  }
  // The main video's live state from the standard media API; not visible in page text.
  let media=null;
  if (video) {
    const tracks=[...(video.textTracks||[])].filter(t=>['captions','subtitles'].includes(t.kind));
    media={speed:video.playbackRate,fullscreen:!!document.fullscreenElement,paused:video.paused,
      muted:video.muted||video.volume===0,captions:tracks.length ? tracks.some(t=>t.mode==='showing') : null};
  }
  for (const k of keys) actions.push({...k, kind:'key'});
  // Only presence flags cross this boundary. Passwords, one-time codes and
  // challenge contents stay in the user's Chrome session.
  const human={
    password:[...document.querySelectorAll('input[type="password"]')].some(visible),
    one_time_code:[...document.querySelectorAll('input')].some(e=>visible(e) && privateCode(e)),
    captcha:[...document.querySelectorAll('iframe,[data-sitekey]')].some(e=>
      visible(e) && /recaptcha|hcaptcha|turnstile|captcha/i.test(
        (e.getAttribute('src')||'')+' '+(e.getAttribute('title')||'')+' '+(e.getAttribute('class')||''))),
  };
  return {url:mask(location.href),title:mask(document.title),w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,human,media};
})()
