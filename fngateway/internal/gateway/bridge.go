package gateway

import "strings"

// BridgeScript 返回注入 HTML 的运行时桥接脚本。
//
// 为什么必须有它：正则只能改写静态 HTML，JS 运行时拼接的 URL（fetch、XHR、
// 动态插入的 iframe、history API 等）不经过 HTML，必须靠脚本在运行时拦截。
// 这是网关适配中最容易漏掉、也最容易导致白屏的一环（见 skill
// references/gateway-proxy.md §4）。本脚本承担三件事：
//
//  1. 前缀改写：所有同源 URL 补上 /app/cli2api，已带前缀的不重复加。
//  2. 鉴权头迁移：Authorization: Bearer <key> → x-api-key。
//     飞牛统一网关（1.2.0604+）会把应用自己的 Authorization 当成非法飞牛
//     票据直接拦截（invalid token），而 cli2api 的鉴权同时接受两者
//     （internal/auth/verifier.go bearerSecret）。控制台前端固定用
//     Authorization（frontend/src/api/client.ts），因此这层迁移是必需的。
//  3. SPA 路由配合：读侧由**构建期**补丁负责（build.py 的
//     patch_console_bundle 改写控制台产物里的 createBrowserLocation，把前缀
//     剥掉再交给路由）；这里只负责写入侧与共享前缀：react-router 写 history
//     时给的是无前缀路径，补上前缀后 URL 栏才可刷新、可深链；同时暴露
//     window.__fnGatewayBase 供产物里的剥离补丁取前缀。
//     为什么读侧不能运行时打补丁：react-router v7 的 history.location 是实时
//     getter，每次匹配都重新读 window.location；而 Chrome 里 Location 的属性是
//     [LegacyUnforgeable]（原型上无 pathname、实例上不可配置），运行时改不掉。
//
// 安全约束（同 skill 要求）：
//   - 同源检查：只改写同源 URL，外链/CDN 不动。
//   - 幂等检查：已带前缀不重复加，避免 /app/cli2api/app/cli2api/...。
//   - 协议白名单：只处理 http/https/ws/wss。
//   - 全程 try/catch：桥接脚本自身报错会让整页白屏，异常必须吞掉。
//   - 全局标记防重复安装。
//
// 该脚本只会经 ${TRIM_APPDEST}/cli2api.sock 注入到 /app/cli2api 前缀下的
// HTML，因此可以假定页面本身位于前缀之下。
func BridgeScript(prefix Prefix) string {
	var b strings.Builder
	b.WriteString(`<script data-fn-gateway-bridge="1">(function(){`)
	b.WriteString(`if(window.__fnGatewayBridgeReady)return;window.__fnGatewayBridgeReady=1;`)
	b.WriteString(`var P=`)
	b.WriteString(jsString(prefix.Path))
	b.WriteString(`;window.__fnGatewayBase=P;`)
	b.WriteString(bridgeBody)
	b.WriteString(`})();</script>`)
	return b.String()
}

// SeedScript 让控制台自身的登录守卫放行，并防止「退出登录」把用户卡在登录页。
//
// 控制台有自己的一套控制台密钥登录（localStorage.cli2api_key，见
// frontend/src/hooks/useApiKey.tsx 与 components/RequireAuth.tsx）。
// 在飞牛上身份由统一网关决定（ui/config 声明 allUsers=false），真实密钥由
// 网关在服务端注入 x-api-key（见 main.go 的 Prepare 钩子），**不下发到浏览器**；
// 这里只放一个占位值让前端守卫通过。
func SeedScript() string {
	return `<script data-fn-gateway-seed="1">(function(){try{` +
		`var K="cli2api_key",S="fnos-gateway-session",ls=window.localStorage;` +
		`ls.setItem(K,S);` +
		`var set=ls.setItem,rm=ls.removeItem;` +
		`ls.setItem=function(k,v){if(k===K&&!String(v==null?"":v).trim()){v=S}return set.call(ls,k,v)};` +
		`ls.removeItem=function(k){if(k===K){return set.call(ls,K,S)}return rm.call(ls,k)};` +
		`}catch(e){}})();</script>`
}

// jsString 安全地把 Go 字符串编码为 JS 字面量（防注入/防提前闭合 script）。
func jsString(s string) string {
	// 只允许网关前缀这种受控字符集；任何异常字符一律 unicode 转义。
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '/', r == '-', r == '_', r == '.', r == '~':
			b.WriteRune(r)
		default:
			const hex = "0123456789abcdef"
			b.WriteString(`\u`)
			b.WriteByte(hex[(r>>12)&0xF])
			b.WriteByte(hex[(r>>8)&0xF])
			b.WriteByte(hex[(r>>4)&0xF])
			b.WriteByte(hex[r&0xF])
		}
	}
	b.WriteByte('"')
	return b.String()
}

// bridgeBody 桥接脚本主体（纯 JS，不含 Go 模板；注意不能出现反引号，
// 因为它被写成 Go 原始字符串）。
const bridgeBody = `
function safe(fn){try{return fn()}catch(e){return null}}
function already(p){return p===P||p.indexOf(P+'/')===0}
function toGw(v){
  if(v===null||v===undefined||v==='')return null;
  var str=String(v).trim();
  if(/^(blob:|data:|javascript:|about:|#)/i.test(str))return null;
  var u; try{u=new URL(str,window.location.href)}catch(e){return null}
  if(!/^(https?|wss?):$/.test(u.protocol))return null;
  if(u.origin!==window.location.origin)return null;
  if(already(u.pathname))return null;
  u.pathname=P+(u.pathname.charAt(0)==='/'?u.pathname:'/'+u.pathname);
  return u;
}
function str(url){var u=toGw(url);return u?u.toString():null}

/* ---- 鉴权头迁移：Authorization -> x-api-key（飞牛网关拦截 Authorization）---- */
function bareSecret(v){
  var s=String(v===null||v===undefined?'':v).trim();
  return s.replace(/^Bearer\s+/i,'');
}
function migrateHeaders(h){
  var out=safe(function(){
    if(!h)return null;
    if(typeof Headers!=='undefined'&&h instanceof Headers){
      var v=h.get('authorization');
      if(v){h.delete('authorization');if(!h.get('x-api-key'))h.set('x-api-key',bareSecret(v));}
      return h;
    }
    var nh=new Headers();
    if(typeof h.forEach==='function'&&!Array.isArray(h)){h.forEach(function(val,key){nh.append(key,val)});}
    else if(Array.isArray(h)){h.forEach(function(pair){if(pair&&pair.length===2)nh.append(pair[0],pair[1])});}
    else if(typeof h==='object'){Object.keys(h).forEach(function(k){nh.append(k,h[k])});}
    else return null;
    var raw=nh.get('authorization');
    if(raw){nh.delete('authorization');if(!nh.get('x-api-key'))nh.set('x-api-key',bareSecret(raw));}
    return nh;
  });
  return out||h;
}

/* fetch */
if(window.fetch){
  var _f=window.fetch;
  window.fetch=function(input,init){
    safe(function(){
      if(typeof input==='string'){
        var s=str(input);if(s)input=s;
        if(init&&init.headers)init.headers=migrateHeaders(init.headers);
        return;
      }
      if(input&&input.url){
        /* Request 对象：url 与 headers 只读，必须重建（保留原方法与头体）。 */
        var s2=str(input.url);
        var h2=input.headers?migrateHeaders(input.headers):null;
        if(s2||h2){
          try{
            input=new Request(s2||input.url,{
              method:input.method,headers:h2||input.headers,body:input.body,
              mode:input.mode,credentials:input.credentials,cache:input.cache,
              redirect:input.redirect,referrer:input.referrer,
              referrerPolicy:input.referrerPolicy,integrity:input.integrity,
              keepalive:input.keepalive,signal:input.signal,duplex:input.duplex
            });
          }catch(e){ /* body 已被消费等情况：退回原始对象，不阻断请求 */ }
        }
        return;
      }
      if(init&&init.headers)init.headers=migrateHeaders(init.headers);
    });
    return _f.call(window,input,init);
  };
}

/* XMLHttpRequest：路径加前缀 + 鉴权头迁移 */
if(window.XMLHttpRequest){
  var _o=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){
    var s=safe(function(){return str(u)});
    if(s)arguments[1]=s;
    return _o.apply(this,arguments);
  };
  if(XMLHttpRequest.prototype.setRequestHeader){
    var _srh=XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader=function(name,value){
      if(typeof name==='string'&&/^authorization$/i.test(name)){
        return _srh.call(this,'x-api-key',bareSecret(value));
      }
      return _srh.call(this,name,value);
    };
  }
}

/* WebSocket：只改写同源同端口的 URL */
if(window.WebSocket){
  var _W=window.WebSocket;
  var W=function(url,protocols){
    var fixed=safe(function(){
      var u=new URL(String(url),window.location.href);
      var wp=(window.location.protocol==='https:'?'wss:':'ws:');
      if(u.protocol!==wp)return null;
      if(u.hostname!==window.location.hostname)return null;
      if(u.port!==window.location.port)return null;
      if(already(u.pathname))return null;
      u.pathname=P+(u.pathname.charAt(0)==='/'?u.pathname:'/'+u.pathname);
      return u.toString();
    });
    return protocols===undefined?new _W(fixed||url):new _W(fixed||url,protocols);
  };
  W.prototype=_W.prototype;
  ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(function(k){try{W[k]=_W[k]}catch(e){}});
  window.WebSocket=W;
}

/* EventSource */
if(window.EventSource){
  var _E=window.EventSource;
  var E=function(url,cfg){
    var fixed=safe(function(){return str(url)});
    return cfg===undefined?new _E(fixed||url):new _E(fixed||url,cfg);
  };
  E.prototype=_E.prototype;
  window.EventSource=E;
}

/* innerHTML / insertAdjacentHTML：只做字符串级属性替换，不执行内容 */
function rewriteHtml(strHtml){
  return safe(function(){
    if(typeof strHtml!=='string'||strHtml.indexOf('/')<0)return strHtml;
    return strHtml.replace(/(\b(?:src|href|action|poster)\s*=\s*["'])(\/(?!\/)[^"']*)/gi,
      function(m,p1,p2){
        if(already(p2))return m;
        return p1+P+p2;
      });
  })||strHtml;
}
try{
  var _ih=Object.getOwnPropertyDescriptor(Element.prototype,'innerHTML');
  if(_ih&&_ih.set){
    var oldIH=_ih.set;
    Object.defineProperty(Element.prototype,'innerHTML',{
      configurable:true,enumerable:_ih.enumerable,get:_ih.get,
      set:function(v){return oldIH.call(this,rewriteHtml(v))}
    });
  }
}catch(e){}
try{
  var _iah=Element.prototype.insertAdjacentHTML;
  Element.prototype.insertAdjacentHTML=function(pos,html){
    return _iah.call(this,pos,rewriteHtml(html));
  };
}catch(e){}

/* Worker 脚本路径 */
try{
  if(window.Worker){
    var _K=window.Worker;
    var K=function(url,opts){
      var fixed=safe(function(){return str(url)});
      return opts===undefined?new _K(fixed||url):new _K(fixed||url,opts);
    };
    K.prototype=_K.prototype;
    window.Worker=K;
  }
}catch(e){}

/* 读侧前缀剥离改为构建期补丁（见文件头第 3 条）：Chrome 把 Location 的属性定义成
   [LegacyUnforgeable]，原型上没有对应描述符、实例上又不配置，运行时改写一律装不上。
   这里不再保留任何针对 Location 原型的补丁，免得看起来像已经接管了 SPA 路由。 */

/* history API：写入加前缀（react-router 的 push/replace 都走这里） */
try{
  var _ps=history.pushState,_rs=history.replaceState;
  function fixState(u){
    return safe(function(){
      if(u===null||u===undefined)return u;
      var s=str(u);return s||u;
    })||u;
  }
  history.pushState=function(st,t,u){return _ps.call(this,st,t,fixState(u))};
  history.replaceState=function(st,t,u){return _rs.call(this,st,t,fixState(u))};
}catch(e){}

/* Element.setAttribute */
try{
  var _sa=Element.prototype.setAttribute;
  Element.prototype.setAttribute=function(n,v){
    if(/^(src|href|action|poster|data)$/i.test(n)){
      var s=safe(function(){return str(v)});if(s)v=s;
    }
    return _sa.call(this,n,v);
  };
}catch(e){}

/* 属性 setter 劫持 */
var PROPS=[['HTMLImageElement','src'],['HTMLImageElement','srcset'],['HTMLLinkElement','href'],
  ['HTMLScriptElement','src'],['HTMLIFrameElement','src'],['HTMLMediaElement','src'],
  ['HTMLVideoElement','poster'],['HTMLSourceElement','src'],['HTMLSourceElement','srcset'],
  ['HTMLFormElement','action'],['HTMLObjectElement','data'],['HTMLEmbedElement','src']];
PROPS.forEach(function(pair){
  try{
    var Ctor=window[pair[0]];if(!Ctor)return;
    var d=Object.getOwnPropertyDescriptor(Ctor.prototype,pair[1]);if(!d||!d.set)return;
    var oldSet=d.set;
    Object.defineProperty(Ctor.prototype,pair[1],{
      configurable:true,enumerable:d.enumerable,
      get:d.get,
      set:function(v){
        var s=safe(function(){return str(v)});
        return oldSet.call(this,s||v);
      }
    });
  }catch(e){}
});

/* 整页跳转兜底（<a> 点击，冒泡阶段）。
   只处理路由没接管的裸 <a>：冒泡阶段保证 react-router 的 Link 处理器已经跑过，
   ev.defaultPrevented 为真就说明是 SPA 内导航，必须让给它；否则（普通外链式
   <a href="/xxx">）补前缀后整页跳转，避免落到飞牛网关的 404 上。 */
document.addEventListener('click',function(ev){
  if(ev.defaultPrevented)return;
  var a=safe(function(){
    var el=ev.target;
    while(el&&el.tagName!=='A')el=el.parentElement;
    return el;
  });
  if(!a||!a.getAttribute)return;
  var href=a.getAttribute('href');
  if(!href||href.charAt(0)==='#'||/^[a-z]+:/i.test(href))return;
  if(a.target&&a.target!=='_self')return;
  var s=safe(function(){return str(href)});
  if(s)ev.preventDefault(),window.location.assign(s);
},false);

/* 动态插入的 iframe 递归安装桥接 */
safe(function(){
  if(!window.MutationObserver)return;
  new MutationObserver(function(muts){
    muts.forEach(function(m){
      Array.prototype.forEach.call(m.addedNodes||[],function(n){
        if(n&&n.tagName==='IFRAME'){safe(function(){
          n.addEventListener('load',function(){safe(function(){
            try{if(n.contentWindow&&!n.contentWindow.__fnGatewayBridgeReady){
              n.contentWindow.__fnGatewayBridgeReady=1;
            }}catch(e){}
          })});
        })}
      });
    });
  }).observe(document.documentElement,{childList:true,subtree:true});
});
`
