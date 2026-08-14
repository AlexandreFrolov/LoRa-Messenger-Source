const messagesEl = document.getElementById('messages');
const statusEl = document.getElementById('status');
const nickEl = document.getElementById('nick');
const textEl = document.getElementById('text');
const sendBtn = document.getElementById('sendBtn');

// ник запоминаем в localStorage браузера (это обычная веб-страница на плате,
// а не превью-артефакт — тут localStorage работает штатно)
nickEl.value = localStorage.getItem('lora_nick') || 'guest';
nickEl.addEventListener('change', () => localStorage.setItem('lora_nick', nickEl.value));

let ws;
let myNick = nickEl.value;

function addMessage(nick, text, opts = {}) {
  const el = document.createElement('div');
  el.className = 'msg' + (opts.own ? ' own' : '');
  const badge = opts.radio ? '<span class="badge">LoRa</span>' : (opts.own ? '' : '<span class="badge">лок.</span>');
  el.innerHTML = `<div class="nick">${escapeHtml(nick)}${badge}</div><div class="text"></div>`;
  el.querySelector('.text').textContent = text;
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    statusEl.textContent = 'онлайн';
    statusEl.className = 'status online';
  };
  ws.onclose = () => {
    statusEl.textContent = 'офлайн';
    statusEl.className = 'status offline';
    setTimeout(connect, 1500); // переподключение
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (e) { return; }

    if (msg.type === 'history') {
      messagesEl.innerHTML = '';
      msg.items.forEach(it => addMessage(it.nick, it.text, { own: it.nick === myNick }));
    } else if (msg.type === 'message') {
      addMessage(msg.nick, msg.text, { own: msg.nick === myNick, radio: msg.radio });
    } else if (msg.type === 'status') {
      console.log('status:', msg.text);
    }
  };
}

function sendMessage() {
  const text = textEl.value.trim();
  if (!text) return;
  myNick = nickEl.value.trim() || 'guest';
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'send', nick: myNick, text }));
    textEl.value = '';
  }
}

sendBtn.addEventListener('click', sendMessage);
textEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendMessage(); });

connect();
