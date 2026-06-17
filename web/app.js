const form = document.getElementById('resume-form');
const fileInput = document.getElementById('gcode-file');
const fileLabel = document.getElementById('file-label');
const fileDrop = document.getElementById('file-drop');
const fileInfo = document.getElementById('file-info');
const printBounds = document.getElementById('print-bounds');
const layerSelect = document.getElementById('layer-select');
const stoppedZInput = document.getElementById('stopped-z');
const currentZInput = document.getElementById('current-z');
const resultSection = document.getElementById('result');
const resultMeta = document.getElementById('result-meta');
const preview = document.getElementById('preview');
const downloadBtn = document.getElementById('download-btn');
const copyBtn = document.getElementById('copy-btn');
const submitBtn = document.getElementById('submit-btn');
const pathPreviewSection = document.getElementById('path-preview');
const previewCanvas = document.getElementById('preview-canvas');
const layerSlider = document.getElementById('layer-slider');
const layerSliderValue = document.getElementById('layer-slider-value');
const previewResetBtn = document.getElementById('preview-reset-btn');
const joinBanner = document.getElementById('join-banner');
const viewModeInputs = document.querySelectorAll('input[name="view-mode"]');

let outputGcode = '';
let fileAnalysis = null;
let pathPreview = null;

const COLORS = {
  printed: '#9ca3af',
  travel: '#3b82f6',
  zHop: '#93c5fd',
  continuation: '#f97316',
  travelAbove: '#cbd5e1',
  grid: '#e5e7eb',
};

class PathPreviewRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.data = null;
    this.viewMode = 'xy';
    this.maxZFilter = Infinity;
    this.scale = 1;
    this.offsetX = 0;
    this.offsetY = 0;
    this.dragging = false;
    this.lastX = 0;
    this.lastY = 0;
    this.fitBounds = null;

    canvas.addEventListener('mousedown', (e) => this.onPointerDown(e.offsetX, e.offsetY));
    canvas.addEventListener('mousemove', (e) => this.onPointerMove(e.offsetX, e.offsetY));
    canvas.addEventListener('mouseup', () => { this.dragging = false; });
    canvas.addEventListener('mouseleave', () => { this.dragging = false; });
    canvas.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
  }

  setData(preview) {
    this.data = preview;
    this.maxZFilter = preview.bounds.max_z;
    this.fitBounds = { ...preview.bounds };
    this.resetView();
  }

  setViewMode(mode) {
    this.viewMode = mode;
    this.resetView();
  }

  setMaxZ(z) {
    this.maxZFilter = z;
    this.draw();
  }

  resetView() {
    if (!this.data) return;
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.floor(rect.width * dpr);
    this.canvas.height = Math.floor(Math.min(rect.width * 0.55, 480) * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const bounds = this.getViewBounds();
    const pad = 24;
    const w = bounds.maxX - bounds.minX || 1;
    const h = bounds.maxY - bounds.minY || 1;
    const cw = rect.width;
    const ch = this.canvas.height / dpr;
    this.scale = Math.min((cw - pad * 2) / w, (ch - pad * 2) / h);
    this.offsetX = pad - bounds.minX * this.scale + (cw - pad * 2 - w * this.scale) / 2;
    this.offsetY = ch - pad + bounds.minY * this.scale - (ch - pad * 2 - h * this.scale) / 2;
    this.draw();
  }

  getViewBounds() {
    const b = this.data.bounds;
    if (this.viewMode === 'xz') {
      return { minX: b.min_x, maxX: b.max_x, minY: b.min_z, maxY: b.max_z };
    }
    return { minX: b.min_x, maxX: b.max_x, minY: b.min_y, maxY: b.max_y };
  }

  toScreen(x, y) {
    if (this.viewMode === 'xz') {
      return {
        x: x * this.scale + this.offsetX,
        y: this.offsetY - y * this.scale,
      };
    }
    return {
      x: x * this.scale + this.offsetX,
      y: this.offsetY - y * this.scale,
    };
  }

  segVisible(seg) {
    return Math.min(seg.z1, seg.z2) <= this.maxZFilter + 1e-6;
  }

  drawSegment(seg, color, width) {
    if (!this.segVisible(seg)) return;
    const p1 = this.toScreen(this.viewMode === 'xz' ? seg.x1 : seg.x1, this.viewMode === 'xz' ? seg.z1 : seg.y1);
    const p2 = this.toScreen(this.viewMode === 'xz' ? seg.x2 : seg.x2, this.viewMode === 'xz' ? seg.z2 : seg.y2);
    this.ctx.strokeStyle = color;
    this.ctx.lineWidth = width;
    this.ctx.beginPath();
    this.ctx.moveTo(p1.x, p1.y);
    this.ctx.lineTo(p2.x, p2.y);
    this.ctx.stroke();
  }

  drawDot(x, y, color, radius) {
    const p = this.toScreen(x, y);
    this.ctx.fillStyle = color;
    this.ctx.beginPath();
    this.ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
    this.ctx.fill();
    this.ctx.strokeStyle = '#fff';
    this.ctx.lineWidth = 1.5;
    this.ctx.stroke();
  }

  draw() {
    if (!this.data) return;
    const rect = this.canvas.getBoundingClientRect();
    const w = rect.width;
    const h = this.canvas.height / (window.devicePixelRatio || 1);
    this.ctx.clearRect(0, 0, w, h);
    this.ctx.fillStyle = '#fafafa';
    this.ctx.fillRect(0, 0, w, h);

    const { printed_segments, resume_segments, continuation_segments, join_info } = this.data;

    printed_segments.forEach((seg) => {
      this.drawSegment(seg, COLORS.printed, 1.5);
    });

    resume_segments.forEach((seg) => {
      const color = seg.type === 'z_hop' ? COLORS.zHop : COLORS.travel;
      this.drawSegment(seg, color, 2);
    });

    continuation_segments.forEach((seg) => {
      if (seg.type === 'extrusion') {
        this.drawSegment(seg, COLORS.continuation, 1.5);
      } else if (seg.type === 'z_hop') {
        this.drawSegment(seg, COLORS.zHop, 1);
      } else {
        this.drawSegment(seg, COLORS.travelAbove, 1);
      }
    });

    if (join_info.last_printed_point && join_info.first_resume_extrusion_point && join_info.gap_mm > 0.5) {
      const a = join_info.last_printed_point;
      const b = join_info.first_resume_extrusion_point;
      this.drawSegment(
        { x1: a.x, y1: a.y, z1: a.z, x2: b.x, y2: b.y, z2: b.z },
        '#ef4444',
        1.5
      );
    }

    if (join_info.current_position) {
      const pos = join_info.current_position;
      const py = this.viewMode === 'xz' ? pos.z : pos.y;
      this.drawDot(pos.x, py, '#ef4444', 5);
    }

    if (join_info.first_resume_extrusion_point) {
      const pt = join_info.first_resume_extrusion_point;
      const py = this.viewMode === 'xz' ? pt.z : pt.y;
      this.drawDot(pt.x, py, '#22c55e', 5);
    }
  }

  onPointerDown(x, y) {
    this.dragging = true;
    this.lastX = x;
    this.lastY = y;
  }

  onPointerMove(x, y) {
    if (!this.dragging) return;
    this.offsetX += x - this.lastX;
    this.offsetY += y - this.lastY;
    this.lastX = x;
    this.lastY = y;
    this.draw();
  }

  onWheel(e) {
    e.preventDefault();
    const factor = e.deltaY > 0 ? 0.9 : 1.1;
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.offsetX;
    const my = e.offsetY;
    this.offsetX = mx - (mx - this.offsetX) * factor;
    this.offsetY = my - (my - this.offsetY) * factor;
    this.scale *= factor;
    this.draw();
  }
}

function setupPathPreview(preview) {
  if (!pathPreview) {
    pathPreview = new PathPreviewRenderer(previewCanvas);
    layerSlider.addEventListener('input', () => {
      const z = parseFloat(layerSlider.value);
      layerSliderValue.textContent = `${z.toFixed(2)} mm`;
      pathPreview.setMaxZ(z);
    });
    previewResetBtn.addEventListener('click', () => pathPreview.resetView());
    viewModeInputs.forEach((input) => {
      input.addEventListener('change', () => {
        if (input.checked) pathPreview.setViewMode(input.value);
      });
    });
    window.addEventListener('resize', () => {
      if (pathPreview.data) pathPreview.resetView();
    });
  }

  const { bounds, join_info } = preview;
  layerSlider.min = bounds.min_z;
  layerSlider.max = bounds.max_z;
  layerSlider.step = 0.01;
  layerSlider.value = bounds.max_z;
  layerSliderValue.textContent = `${bounds.max_z.toFixed(2)} mm`;

  pathPreview.setData(preview);
  pathPreviewSection.classList.remove('hidden');

  if (join_info.gap_mm > 0.5) {
    joinBanner.textContent =
      `Join gap: ${join_info.gap_mm.toFixed(2)} mm between last printed extrusion and resume start — verify this is acceptable.`;
    joinBanner.classList.remove('hidden');
  } else {
    joinBanner.classList.add('hidden');
  }
}

function clearElement(el) {
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }
}

function appendLine(parent, text, className) {
  const el = document.createElement(className || 'div');
  el.textContent = text;
  parent.appendChild(el);
  return el;
}

function populateLayerSelect(layers) {
  while (layerSelect.options.length > 1) {
    layerSelect.remove(1);
  }

  layers.forEach((z, index) => {
    const option = document.createElement('option');
    option.value = String(z);
    option.textContent = `Layer ${index + 1} — Z ${z.toFixed(2)} mm`;
    layerSelect.appendChild(option);
  });
}

async function analyzeFile(file) {
  const formData = new FormData();
  formData.append('gcode', file);

  const res = await fetch('/api/analyze', { method: 'POST', body: formData });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || 'Could not analyze G-code');
  }
  return data;
}

async function handleFileSelected(file) {
  if (!file) return;

  fileLabel.textContent = file.name;
  fileAnalysis = null;
  fileInfo.hidden = true;

  try {
    fileAnalysis = await analyzeFile(file);
    const { layers, min_z, max_z, min_x, max_x, min_y, max_y } = fileAnalysis;

    printBounds.textContent =
      `${layers.length} layers detected · Z ${min_z.toFixed(2)}–${max_z.toFixed(2)} mm · ` +
      `XY ${min_x.toFixed(1)}–${max_x.toFixed(1)} / ${min_y.toFixed(1)}–${max_y.toFixed(1)} mm`;

    populateLayerSelect(layers);
    fileInfo.hidden = false;
  } catch (err) {
    alert(err.message);
  }
}

fileInput.addEventListener('change', () => {
  handleFileSelected(fileInput.files[0]);
});

layerSelect.addEventListener('change', () => {
  if (!layerSelect.value) return;
  const z = parseFloat(layerSelect.value);
  stoppedZInput.value = z.toFixed(2);
  if (!currentZInput.value) {
    currentZInput.value = z.toFixed(2);
  }
});

['dragenter', 'dragover'].forEach((event) => {
  fileDrop.addEventListener(event, (e) => {
    e.preventDefault();
    fileDrop.classList.add('dragover');
  });
});

['dragleave', 'drop'].forEach((event) => {
  fileDrop.addEventListener(event, (e) => {
    e.preventDefault();
    fileDrop.classList.remove('dragover');
  });
});

fileDrop.addEventListener('drop', (e) => {
  const file = e.dataTransfer.files[0];
  if (file) {
    fileInput.files = e.dataTransfer.files;
    handleFileSelected(file);
  }
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();

  const file = fileInput.files[0];
  if (!file) {
    alert('Please select a G-code file.');
    return;
  }

  const formData = new FormData();
  formData.append('gcode', file);
  formData.append('stopped_z', stoppedZInput.value);
  formData.append('current_x', document.getElementById('current-x').value);
  formData.append('current_y', document.getElementById('current-y').value);
  formData.append('current_z', currentZInput.value);
  formData.append('z_lift', document.getElementById('z-lift').value);
  formData.append(
    'require_extrusion',
    document.getElementById('require-extrusion').checked ? 'true' : 'false'
  );

  submitBtn.disabled = true;
  submitBtn.textContent = 'Processing…';

  try {
    const res = await fetch('/api/resume', { method: 'POST', body: formData });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.error || 'Request failed');
    }

    outputGcode = data.gcode;
    const lines = data.gcode.split('\n');
    preview.textContent = lines.slice(0, 80).join('\n') + (lines.length > 80 ? '\n…' : '');

    clearElement(resultMeta);
    const title = document.createElement('strong');
    title.textContent = `Resume at line ${data.resume_line}`;
    resultMeta.appendChild(title);

    if (data.resume_x != null) {
      appendLine(
        resultMeta,
        `Target position: X=${data.resume_x.toFixed(3)} Y=${data.resume_y.toFixed(3)} Z=${data.resume_z.toFixed(3)}`
      );
    }
    appendLine(resultMeta, `Removed ${data.lines_removed} lines from the start`);
    if (data.hotend_temp) appendLine(resultMeta, `Hotend: ${data.hotend_temp}°C`);
    if (data.bed_temp) appendLine(resultMeta, `Bed: ${data.bed_temp}°C`);

    const warnings = data.warnings || (data.warning ? [data.warning] : []);
    warnings.forEach((message) => {
      const warn = appendLine(resultMeta, `⚠ ${message}`, 'div');
      warn.className = 'warning';
    });

    if (data.preview) {
      setupPathPreview(data.preview);
    }

    resultSection.classList.remove('hidden');
    resultSection.scrollIntoView({ behavior: 'smooth' });
  } catch (err) {
    alert(err.message);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Generate resume G-code';
  }
});

downloadBtn.addEventListener('click', () => {
  if (!outputGcode) return;
  const blob = new Blob([outputGcode], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'resume.gcode';
  a.click();
  URL.revokeObjectURL(url);
});

copyBtn.addEventListener('click', async () => {
  if (!outputGcode) return;
  try {
    await navigator.clipboard.writeText(outputGcode);
    copyBtn.textContent = 'Copied!';
    setTimeout(() => { copyBtn.textContent = 'Copy to clipboard'; }, 2000);
  } catch {
    alert('Could not copy to clipboard.');
  }
});
