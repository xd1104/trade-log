/* 極簡 CDP 客戶端（Node 22 內建 WebSocket，零相依） */
export class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = new Map();
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id != null && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? reject(new Error(m.method + " " + JSON.stringify(m.error))) : resolve(m.result);
      } else if (m.method) {
        const hs = this.handlers.get(m.method) || [];
        for (const h of hs) h(m.params);
      }
    });
  }
  static async attach(port) {
    const ver = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = ver.find(t => t.type === "page");
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener("open", res); ws.addEventListener("error", rej); });
    return new CDP(ws);
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject, method }));
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
  close() { this.ws.close(); }
}
