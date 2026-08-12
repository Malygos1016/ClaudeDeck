// 极简单系列日折线图(canvas,DPR 适配)。
// 纪律(dataviz):单轴;2px 细线;网格退后;文本用文字色不用系列色;
// 悬浮十字线+tooltip 默认必配;悬浮点带 2px 底色环;空态给指引。

const INK_DIM = "#8d97a7";
const INK_FAINT = "#5f6a7a";
const GRID = "rgba(43, 53, 66, 0.8)";
const SURFACE = "#1a212b";

export function lineChart(canvas, tipEl, opts) {
  const { points, color, fmtY, fmtTip } = opts; // points: [{date, value, extra?}]
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 460;
  const cssH = canvas.clientHeight || 150;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  ctx.scale(dpr, dpr);

  const pad = { l: 46, r: 10, t: 10, b: 20 };
  const W = cssW - pad.l - pad.r;
  const H = cssH - pad.t - pad.b;

  ctx.clearRect(0, 0, cssW, cssH);
  ctx.font = '10px "Cascadia Code", Consolas, monospace';

  if (!points.length) {
    ctx.fillStyle = INK_FAINT;
    ctx.textAlign = "center";
    ctx.fillText("窗口期内无记录", cssW / 2, cssH / 2);
    return { destroy() {} };
  }

  const maxV = Math.max(...points.map((p) => p.value), 1);
  const x = (i) => pad.l + (points.length === 1 ? W / 2 : (i / (points.length - 1)) * W);
  const y = (v) => pad.t + H - (v / maxV) * H;

  // 网格(3 条,退后)+ y 轴刻度文本
  ctx.strokeStyle = GRID;
  ctx.lineWidth = 1;
  ctx.fillStyle = INK_FAINT;
  ctx.textAlign = "right";
  for (const f of [0, 0.5, 1]) {
    const gy = pad.t + H - f * H;
    ctx.beginPath();
    ctx.moveTo(pad.l, gy);
    ctx.lineTo(pad.l + W, gy);
    ctx.stroke();
    ctx.fillText(fmtY(maxV * f), pad.l - 6, gy + 3);
  }
  // x 轴首末日期
  ctx.textAlign = "left";
  ctx.fillText(points[0].date.slice(5), pad.l, cssH - 6);
  if (points.length > 1) {
    ctx.textAlign = "right";
    ctx.fillText(points[points.length - 1].date.slice(5), pad.l + W, cssH - 6);
  }

  // 数据线(2px)
  const drawSeries = () => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    points.forEach((p, i) => {
      const px = x(i), py = y(p.value);
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.stroke();
    if (points.length === 1) {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x(0), y(points[0].value), 3, 0, Math.PI * 2);
      ctx.fill();
    }
  };
  drawSeries();
  const base = ctx.getImageData(0, 0, canvas.width, canvas.height);

  // 悬浮:十字线 + 数据点(2px 底色环) + tooltip
  const onMove = (ev) => {
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const i = Math.max(
      0,
      Math.min(points.length - 1, Math.round(((mx - pad.l) / Math.max(W, 1)) * (points.length - 1)))
    );
    // putImageData 按设备像素还原底图,不受 transform 影响;后续绘制仍走 dpr 缩放后的 CSS 坐标
    ctx.putImageData(base, 0, 0);
    const px = x(i), py = y(points[i].value);
    ctx.strokeStyle = GRID;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, pad.t);
    ctx.lineTo(px, pad.t + H);
    ctx.stroke();
    ctx.fillStyle = SURFACE; // 2px 底色环
    ctx.beginPath();
    ctx.arc(px, py, 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px, py, 4, 0, Math.PI * 2);
    ctx.fill();

    tipEl.hidden = false;
    tipEl.innerHTML = fmtTip(points[i]);
    const tw = tipEl.offsetWidth;
    let left = px + 12;
    if (left + tw > cssW - 4) left = px - tw - 12;
    tipEl.style.left = `${Math.max(0, left)}px`;
    tipEl.style.top = `${Math.max(0, py - 34)}px`;
  };
  const onLeave = () => {
    ctx.putImageData(base, 0, 0);
    tipEl.hidden = true;
  };
  canvas.addEventListener("mousemove", onMove);
  canvas.addEventListener("mouseleave", onLeave);
  return {
    destroy() {
      canvas.removeEventListener("mousemove", onMove);
      canvas.removeEventListener("mouseleave", onLeave);
      tipEl.hidden = true;
    },
  };
}
