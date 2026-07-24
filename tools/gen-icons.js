// 產生 trade-log PWA icon（純 Node，無外部依賴）。輸出到 ../icons。
// 設計 A「走勢箭頭」：深色圓角底 + 金色上升折線 + 箭頭，象徵獲利/向上。
// 用法：node tools/gen-icons.js  （在 repo 根目錄執行）
const zlib = require('zlib');
const fs = require('fs');
const path = require('path');

const GOLD = [0xE3, 0xA9, 0x51];
const DARK_TOP = [0x10, 0x14, 0x1b], DARK_BOT = [0x1a, 0x20, 0x2b];
const lerp = (a, b, t) => a + (b - a) * t;

function render(N) {
  const buf = new Uint8Array(N * N * 4);
  const set = (x, y, c, a) => {
    x = Math.round(x); y = Math.round(y);
    if (x < 0 || y < 0 || x >= N || y >= N) return;
    const i = (y * N + x) * 4, ia = a / 255;
    buf[i]   = Math.round(lerp(buf[i], c[0], ia));
    buf[i+1] = Math.round(lerp(buf[i+1], c[1], ia));
    buf[i+2] = Math.round(lerp(buf[i+2], c[2], ia));
    buf[i+3] = 255;
  };
  const line = (x0, y0, x1, y1, c, w) => {
    const half = w / 2, dx = x1 - x0, dy = y1 - y0, l2 = dx*dx + dy*dy || 1;
    for (let y = Math.floor(Math.min(y0,y1)-half-2); y <= Math.max(y0,y1)+half+2; y++)
      for (let x = Math.floor(Math.min(x0,x1)-half-2); x <= Math.max(x0,x1)+half+2; x++) {
        let t = ((x-x0)*dx + (y-y0)*dy)/l2; t = Math.max(0, Math.min(1, t));
        const d = Math.hypot(x - (x0+t*dx), y - (y0+t*dy));
        if (d <= half) set(x, y, c, 255);
        else if (d <= half + 1.2) set(x, y, c, 255*(1-(d-half)/1.2));
      }
  };
  const tri = (p, c) => {
    const sg = (ax,ay,bx,by,cx,cy) => (ax-cx)*(by-cy)-(bx-cx)*(ay-cy);
    const xs = p.map(q=>q[0]), ys = p.map(q=>q[1]);
    for (let y = Math.floor(Math.min(...ys)); y <= Math.max(...ys); y++)
      for (let x = Math.floor(Math.min(...xs)); x <= Math.max(...xs); x++) {
        const d1=sg(x,y,p[0][0],p[0][1],p[1][0],p[1][1]),
              d2=sg(x,y,p[1][0],p[1][1],p[2][0],p[2][1]),
              d3=sg(x,y,p[2][0],p[2][1],p[0][0],p[0][1]);
        if (!(((d1<0)||(d2<0)||(d3<0))&&((d1>0)||(d2>0)||(d3>0)))) set(x, y, c, 255);
      }
  };

  // 背景漸層
  for (let y = 0; y < N; y++) {
    const t = y/(N-1), c = [Math.round(lerp(DARK_TOP[0],DARK_BOT[0],t)), Math.round(lerp(DARK_TOP[1],DARK_BOT[1],t)), Math.round(lerp(DARK_TOP[2],DARK_BOT[2],t))];
    for (let x = 0; x < N; x++) { const i=(y*N+x)*4; buf[i]=c[0];buf[i+1]=c[1];buf[i+2]=c[2];buf[i+3]=255; }
  }

  const P = [[.16,.68],[.34,.60],[.46,.66],[.62,.42],[.80,.28]].map(([x,y]) => [x*N, y*N]);

  // 折線下方淡金面積
  for (let x = P[0][0]; x <= P[P.length-1][0]; x++) {
    let yl = null;
    for (let s=0;s<P.length-1;s++){const a=P[s],b=P[s+1];if(x>=a[0]&&x<=b[0]){yl=lerp(a[1],b[1],(x-a[0])/(b[0]-a[0]||1));break;}}
    if (yl == null) continue;
    for (let y = Math.ceil(yl); y < N; y++) set(x, y, GOLD, 22*Math.max(0, 1-(y-yl)/(N-yl)));
  }
  // 粗折線
  for (let s = 0; s < P.length-1; s++) line(P[s][0], P[s][1], P[s+1][0], P[s+1][1], GOLD, N*0.028);
  // 端點箭頭
  const A = P[P.length-2], B = P[P.length-1];
  let dx = B[0]-A[0], dy = B[1]-A[1]; const dl = Math.hypot(dx,dy) || 1; dx/=dl; dy/=dl;
  const px = -dy, py = dx, hl = N*0.11, hh = N*0.062;
  const tip = [B[0]+dx*hl*0.3, B[1]+dy*hl*0.3], bc = [tip[0]-dx*hl, tip[1]-dy*hl];
  tri([tip, [bc[0]+px*hh, bc[1]+py*hh], [bc[0]-px*hh, bc[1]-py*hh]], GOLD);

  return buf;
}

function encodePNG(N, rgba) {
  const sig = Buffer.from([137,80,78,71,13,10,26,10]);
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0);
    const t = Buffer.from(type, 'ascii'), crc = Buffer.alloc(4);
    crc.writeUInt32BE(zlib.crc32(Buffer.concat([t, data])) >>> 0, 0);
    return Buffer.concat([len, t, data, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(N,0); ihdr.writeUInt32BE(N,4); ihdr[8]=8; ihdr[9]=6;
  const raw = Buffer.alloc((N*4+1)*N);
  for (let y=0;y<N;y++){ raw[y*(N*4+1)]=0; rgba.subarray(y*N*4,(y+1)*N*4).forEach((v,i)=>{raw[y*(N*4+1)+1+i]=v;}); }
  const idat = zlib.deflateSync(raw, { level: 9 });
  return Buffer.concat([sig, chunk('IHDR',ihdr), chunk('IDAT',idat), chunk('IEND',Buffer.alloc(0))]);
}

const outDir = path.join(__dirname, '..', 'icons');
fs.mkdirSync(outDir, { recursive: true });
[['icon-512.png',512], ['icon-192.png',192], ['apple-touch-icon.png',180]].forEach(([name, N]) => {
  const png = encodePNG(N, render(N));
  fs.writeFileSync(path.join(outDir, name), png);
  console.log('wrote', name, N + 'x' + N, png.length + 'B');
});
