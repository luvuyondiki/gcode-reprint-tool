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

let outputGcode = '';
let fileAnalysis = null;

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
