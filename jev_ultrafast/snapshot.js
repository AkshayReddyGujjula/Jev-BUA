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
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
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
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),mask(name(e)),mask(e.value??null),e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      mask(e.getAttribute('href')),mask(scope?.innerText?.slice(0,6000)||'')];
  };
  const actions=[];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:mask(name(e)||rname),
      form_submit:(!!e.form && e.type==='submit'),
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    for (const key of ['checked','selected','expanded']) {
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
  if (document.querySelector('video')) keys.push(
    {id:'key_fullscreen',key:'f',code:'KeyF',vk:70,label:'Toggle fullscreen (F)'},
    {id:'key_captions',key:'c',code:'KeyC',vk:67,label:'Toggle captions (C)'},
    {id:'key_play',key:'k',code:'KeyK',vk:75,label:'Play or pause (K)'},
    {id:'key_mute',key:'m',code:'KeyM',vk:77,label:'Mute (M)'},
    {id:'key_speed_up',key:'>',code:'Period',vk:190,shift:true,label:'Increase playback speed (Shift+.)'},
    {id:'key_speed_down',key:'<',code:'Comma',vk:188,shift:true,label:'Decrease playback speed (Shift+,)'},
    {id:'key_seek_forward',key:'l',code:'KeyL',vk:76,label:'Seek forward 10 seconds (L)'},
    {id:'key_seek_backward',key:'j',code:'KeyJ',vk:74,label:'Seek backward 10 seconds (J)'});
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
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,human};
})()
