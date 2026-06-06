/* Bitcoin Argus — a tiny dependency-free canvas line chart for the /stats page.
 *
 * No third-party library: the image stays self-contained (and works over Tor /
 * offline), and the chart reads its colours from the active theme's CSS custom
 * properties so it matches hacker / game / bootstrap without per-theme JS. The
 * data volume here is small (a few series over a few hundred points), so a hand
 * -rolled renderer is lighter and simpler than pulling in a charting framework.
 *
 * Usage:
 *   const c = new LineChart(canvasEl, { yFormat: fmtBytes, xFormat: fmtTime });
 *   c.setData([{ label: 'lnd', color: '#6ea0ff', data: [[tsSeconds, value], ...] }]);
 * It re-renders on container resize and shows a crosshair tooltip on hover.
 */
(function (global) {
  "use strict";

  function cssVar(el, name, fallback) {
    var v = getComputedStyle(el).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  // "Nice" axis step so gridlines land on round numbers.
  function niceStep(range, targetTicks) {
    if (range <= 0) return 1;
    var raw = range / Math.max(1, targetTicks);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    var step = norm >= 5 ? 5 : norm >= 2 ? 2 : 1;
    return step * mag;
  }

  function LineChart(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = opts || {};
    this.series = [];
    this.hover = null;
    var self = this;
    canvas.addEventListener("mousemove", function (e) {
      var r = canvas.getBoundingClientRect();
      self.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
      self.render();
    });
    canvas.addEventListener("mouseleave", function () {
      self.hover = null;
      self.render();
    });
    if (global.ResizeObserver) {
      new ResizeObserver(function () {
        self.render();
      }).observe(canvas);
    } else {
      global.addEventListener("resize", function () {
        self.render();
      });
    }
  }

  LineChart.prototype.setData = function (series) {
    this.series = series || [];
    this.render();
  };

  LineChart.prototype.render = function () {
    var canvas = this.canvas,
      ctx = this.ctx,
      opts = this.opts;
    var dpr = global.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 600,
      cssH = canvas.clientHeight || 240;
    if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var fg = cssVar(canvas, "--fg", "#ccc");
    var faint = cssVar(canvas, "--line", "#444");
    var bright = cssVar(canvas, "--fg-bright", "#fff");

    var padL = 64,
      padR = 12,
      padT = 10,
      padB = 26;
    var plotW = Math.max(10, cssW - padL - padR);
    var plotH = Math.max(10, cssH - padT - padB);

    // Bounds across all series.
    var xMin = Infinity,
      xMax = -Infinity,
      yMin = 0,
      yMax = -Infinity;
    this.series.forEach(function (s) {
      s.data.forEach(function (p) {
        if (p[0] < xMin) xMin = p[0];
        if (p[0] > xMax) xMax = p[0];
        if (p[1] > yMax) yMax = p[1];
        if (p[1] < yMin) yMin = p[1];
      });
    });
    if (!isFinite(xMin)) {
      ctx.fillStyle = fg;
      ctx.font = "13px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("no data yet", cssW / 2, cssH / 2);
      return;
    }
    if (xMax === xMin) xMax = xMin + 1;
    if (yMax <= yMin) yMax = yMin + 1;
    yMax *= 1.08; // headroom

    var xFor = function (t) {
      return padL + ((t - xMin) / (xMax - xMin)) * plotW;
    };
    var yFor = function (v) {
      return padT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;
    };
    var yFormat =
      opts.yFormat ||
      function (v) {
        return String(Math.round(v));
      };
    var xFormat =
      opts.xFormat ||
      function (t) {
        return String(t);
      };

    // Y gridlines + labels.
    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    var stepY = niceStep(yMax - yMin, 4);
    ctx.strokeStyle = faint;
    ctx.fillStyle = fg;
    ctx.lineWidth = 1;
    for (var gy = Math.ceil(yMin / stepY) * stepY; gy <= yMax; gy += stepY) {
      var py = yFor(gy);
      ctx.globalAlpha = 0.25;
      ctx.beginPath();
      ctx.moveTo(padL, py);
      ctx.lineTo(padL + plotW, py);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.textAlign = "right";
      ctx.fillText(yFormat(gy), padL - 6, py);
    }

    // X labels (a handful).
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var ticks = 5;
    for (var i = 0; i <= ticks; i++) {
      var t = xMin + ((xMax - xMin) * i) / ticks;
      var px = xFor(t);
      ctx.fillStyle = fg;
      ctx.fillText(xFormat(t), px, padT + plotH + 6);
    }

    // Series lines.
    ctx.lineWidth = 1.6;
    this.series.forEach(function (s) {
      if (!s.data.length) return;
      ctx.strokeStyle = s.color;
      ctx.beginPath();
      s.data.forEach(function (p, idx) {
        var px = xFor(p[0]),
          py = yFor(p[1]);
        if (idx === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    });

    // Hover crosshair + tooltip.
    if (this.hover && this.hover.x >= padL && this.hover.x <= padL + plotW) {
      var ht = xMin + ((this.hover.x - padL) / plotW) * (xMax - xMin);
      ctx.strokeStyle = bright;
      ctx.globalAlpha = 0.4;
      ctx.beginPath();
      ctx.moveTo(this.hover.x, padT);
      ctx.lineTo(this.hover.x, padT + plotH);
      ctx.stroke();
      ctx.globalAlpha = 1;

      var rows = [];
      this.series.forEach(function (s) {
        if (!s.data.length) return;
        // nearest point by x
        var best = s.data[0],
          bd = Math.abs(s.data[0][0] - ht);
        for (var k = 1; k < s.data.length; k++) {
          var d = Math.abs(s.data[k][0] - ht);
          if (d < bd) {
            bd = d;
            best = s.data[k];
          }
        }
        rows.push({ color: s.color, label: s.label, v: best[1], t: best[0] });
        var cx = xFor(best[0]),
          cy = yFor(best[1]);
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(cx, cy, 2.5, 0, Math.PI * 2);
        ctx.fill();
      });

      if (rows.length) {
        rows.sort(function (a, b) {
          return b.v - a.v;
        });
        var lines = rows.map(function (r) {
          return r.label + "  " + yFormat(r.v);
        });
        lines.unshift(xFormat(rows[0].t));
        ctx.font = "11px system-ui, sans-serif";
        var tw = 0;
        lines.forEach(function (l) {
          tw = Math.max(tw, ctx.measureText(l).width);
        });
        var bw = tw + 22,
          bh = lines.length * 15 + 8;
        var bx = this.hover.x + 12;
        if (bx + bw > cssW) bx = this.hover.x - bw - 12;
        var by = Math.min(padT + 4, cssH - bh - 4);
        ctx.fillStyle = cssVar(canvas, "--panel-top", "rgba(0,0,0,0.85)");
        ctx.globalAlpha = 0.95;
        ctx.fillRect(bx, by, bw, bh);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = faint;
        ctx.strokeRect(bx, by, bw, bh);
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        lines.forEach(function (l, idx) {
          var ly = by + 5 + idx * 15;
          if (idx > 0) {
            ctx.fillStyle = rows[idx - 1].color;
            ctx.fillRect(bx + 6, ly + 3, 8, 8);
            ctx.fillStyle = bright;
            ctx.fillText(l, bx + 18, ly);
          } else {
            ctx.fillStyle = fg;
            ctx.fillText(l, bx + 6, ly);
          }
        });
      }
    }
  };

  global.LineChart = LineChart;
})(window);
