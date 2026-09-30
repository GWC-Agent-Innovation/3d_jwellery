const $ = (id) => document.getElementById(id);
let selected, previewUrl, busy = false, viewer, currentJob, showingSketch = false;
const message = (text) => { $('message').textContent = text; };

function select(file) {
  if (!file || busy) return;
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) return message('Choose a JPG, PNG or WebP image.');
  if (file.size > 10 * 1024 * 1024) return message('Choose an image smaller than 10 MB.');
  selected = file;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  $('preview').src = previewUrl;
  $('preview').hidden = false;
  $('upload-prompt').hidden = true;
  $('filename').textContent = file.name;
  $('change').hidden = false;
  $('generate').disabled = false;
  clearOutput();
  $('empty').hidden = false;
  $('download').hidden = true;
  $('reset').disabled = true;
  $('sketch-toggle').disabled = true;
  $('state-label').textContent = 'READY TO GENERATE';
  $('past-projects').value = '';
  message('');
}
$('image-input').addEventListener('change', (event) => select(event.target.files[0]));
$('change').onclick = () => $('image-input').click();
for (const type of ['dragenter', 'dragover']) $('dropzone').addEventListener(type, (event) => {
  event.preventDefault(); $('dropzone').classList.add('dragging');
});
for (const type of ['dragleave', 'drop']) $('dropzone').addEventListener(type, (event) => {
  event.preventDefault(); $('dropzone').classList.remove('dragging');
  if (type === 'drop') select(event.dataTransfer.files[0]);
});

async function request(url, options = {}) {
  const response = await fetch(url, { ...options, signal: AbortSignal.timeout(30000) });
  let body;
  try { body = await response.json(); } catch { throw new Error('Cannot reach the generation backend. Start the FastAPI server and open its local URL.'); }
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'The request failed. Please try again.');
  return body;
}

$('generate').onclick = async () => {
  if (!selected || busy) return;
  busy = true;
  $('generate').disabled = $('image-input').disabled = $('change').disabled = true;
  $('empty').hidden = true; $('loading').hidden = false;
  $('loading-text').textContent = 'Uploading your image…';
  $('state-label').textContent = 'PROCESSING';
  $('download').hidden = true; $('reset').disabled = true; $('sketch-toggle').disabled = true;
  clearOutput(); message('');
  try {
    const form = new FormData(); form.append('image', selected);
    let job = await request('/api/generations', { method: 'POST', body: form });
    const deadline = Date.now() + 60 * 60 * 1000;
    while (job.status !== 'failed' && (job.status !== 'completed' || ['queued', 'generating'].includes(job.sketch_status))) {
      if (Date.now() > deadline) throw new Error('Generation is taking longer than expected. Check the backend before retrying.');
      $('loading-text').textContent = stageText(job);
      await new Promise(resolve => setTimeout(resolve, 2000));
      job = await request(`/api/generations/${job.job_id}`);
    }
    if (job.status === 'failed') throw new Error(job.error);
    $('loading-text').textContent = 'Opening your 3D model…';
    await showCompletedJob(job);
  } catch (error) {
    $('empty').hidden = false; $('state-label').textContent = 'TRY AGAIN';
    message(error.message || 'Something went wrong. Please try again.');
  } finally {
    busy = false; $('loading').hidden = true;
    $('generate').disabled = $('image-input').disabled = $('change').disabled = false;
  }
};

async function showCompletedJob(job) {
  currentJob = job; showingSketch = false;
  $('sketch-toggle').textContent = 'Show sketch 3D';
  $('download').href = job.model_url; $('download').hidden = false;
  $('download-sketch').hidden = !job.sketch_model_url;
  if (job.sketch_model_url) $('download-sketch').href = job.sketch_model_url;
  $('retry-sketch').hidden = !!job.sketch_model_url || ['queued', 'generating'].includes(job.sketch_status);
  $('retry-sketch').textContent = job.sketch_status === 'failed' ? 'Retry sketch' : 'Generate sketch 3D';
  viewer ||= await createViewer();
  await viewer.load(job.model_url);
  $('empty').hidden = true;
  $('reset').disabled = false;
  $('sketch-toggle').disabled = !job.sketch_model_url;
  $('state-label').textContent = 'READY';
  const url = new URL(location.href);
  url.searchParams.set('job', job.job_id);
  history.replaceState(null, '', url);
  message(job.sketch_model_url ? 'Both 3D models are ready. Switch models, drag to rotate, or download either GLB.' : (job.sketch_error || (['queued', 'generating'].includes(job.sketch_status) ? 'Realistic model ready. The sketch is still generating; reopen this project shortly.' : 'Realistic model ready. Generate a separate sketch model when ready.')));
  if (![...$('past-projects').options].some(option => option.value === job.job_id)) {
    const option = document.createElement('option');
    option.value = job.job_id;
    const when = new Date(job.created_at);
    option.textContent = Number.isNaN(when.getTime()) ? job.job_id.slice(0, 8) : when.toLocaleString();
    $('past-projects').append(option);
  }
  $('past-projects').value = job.job_id;
}

async function openSavedJob(id) {
  if (!/^[a-f0-9]{32}$/.test(id)) return message('Invalid model link.');
  busy = true;
  $('image-input').disabled = true;
  $('empty').hidden = true; $('loading').hidden = false;
  $('loading-text').textContent = 'Opening your saved jewellery model…';
  try {
    const job = await request(`/api/generations/${id}`);
    if (job.status !== 'completed') throw new Error(job.error || 'This model is not ready yet.');
    showSavedInput(job);
    await showCompletedJob(job);
  } catch (error) {
    $('empty').hidden = false;
    $('state-label').textContent = 'UNAVAILABLE';
    message(error.message);
  } finally {
    busy = false; $('image-input').disabled = false; $('loading').hidden = true;
  }
}

function showSavedInput(job) {
  selected = null;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  $('preview').src = job.image_url;
  $('preview').hidden = false;
  $('upload-prompt').hidden = true;
  $('filename').textContent = 'Saved project · ' + job.job_id.slice(0, 8);
  $('change').hidden = false;
  $('generate').disabled = true;
}

async function loadPastProjects() {
  const select = $('past-projects');
  try {
    const jobs = await request('/api/generations');
    for (const job of jobs) {
      const option = document.createElement('option');
      option.value = job.job_id;
      const when = new Date(job.created_at);
      option.textContent = Number.isNaN(when.getTime()) ? job.job_id.slice(0, 8) : when.toLocaleString();
      select.appendChild(option);
    }
  } catch { /* dropdown just stays empty */ }
}
$('past-projects').addEventListener('change', (event) => {
  const id = event.target.value;
  if (!busy && id) openSavedJob(id);
});
loadPastProjects();

function clearOutput() {
  viewer?.clear(); currentJob = null; showingSketch = false;
  $('download-sketch').hidden = true; $('retry-sketch').hidden = true;
  $('sketch-toggle').textContent = 'Show sketch 3D';
}
function stageText(job) {
  return ({realistic_model: 'Creating realistic 3D jewellery...', sketch_image: 'AI is drawing the sketch reference...',
    sketch_model: 'Creating a separate 3D model from the sketch...'})[job.stage] || 'Waiting for free GPU generation...';
}
$('sketch-toggle').onclick = async () => {
  if (busy || !currentJob?.sketch_model_url) return;
  busy = true; $('sketch-toggle').disabled = true;
  const next = !showingSketch;
  try {
    await viewer.load(next ? currentJob.sketch_model_url : currentJob.model_url);
    showingSketch = next;
    $('sketch-toggle').textContent = next ? 'Show realistic 3D' : 'Show sketch 3D';
    $('state-label').textContent = next ? 'SKETCH 3D' : 'REALISTIC 3D';
  } catch { message('Could not load this model. Try switching again.'); }
  finally { busy = false; $('sketch-toggle').disabled = false; }
};
$('retry-sketch').onclick = async () => {
  if (busy || !currentJob) return;
  busy = true; $('retry-sketch').disabled = true; $('sketch-toggle').disabled = true;
  $('image-input').disabled = $('change').disabled = $('generate').disabled = true;
  try {
    let job = await request(`/api/generations/${currentJob.job_id}/sketch`, {method: 'POST'});
    const deadline = Date.now() + 40 * 60 * 1000;
    while (['queued', 'generating'].includes(job.sketch_status)) {
      if (Date.now() > deadline) throw new Error('Sketch is taking longer than expected. Reopen this project later to check its status.');
      message(stageText(job));
      await new Promise(resolve => setTimeout(resolve, 2000));
      job = await request(`/api/generations/${job.job_id}`);
    }
    await showCompletedJob(job);
  } catch (error) { message(error.message); }
  finally {
    busy = false; $('retry-sketch').disabled = false;
    $('image-input').disabled = $('change').disabled = false;
    $('generate').disabled = !selected;
    $('sketch-toggle').disabled = !currentJob?.sketch_model_url;
  }
};

async function createViewer() {
  const THREE = await import('three');
  const [{ OrbitControls }, { GLTFLoader }, { RoomEnvironment }] = await Promise.all([
    import('three/addons/controls/OrbitControls.js'),
    import('three/addons/loaders/GLTFLoader.js'),
    import('three/addons/environments/RoomEnvironment.js'),
  ]);
  const container = $('viewer');
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;
  renderer.domElement.setAttribute('aria-label', 'Interactive 3D jewellery preview');
  container.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  scene.environment = pmrem.fromScene(room, 0.04).texture;
  room.dispose(); pmrem.dispose();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 100);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.enablePan = false;
  controls.minDistance = 0.8; controls.maxDistance = 10;
  let model;
  function reset() { camera.position.set(2.3, 1.4, 3.2); controls.target.set(0, 0, 0); controls.update(); }
  $('reset').onclick = reset;
  const resize = new ResizeObserver(() => {
    const width = container.clientWidth, height = container.clientHeight;
    renderer.setSize(width, height, false); camera.aspect = width / height; camera.updateProjectionMatrix();
  });
  resize.observe(container);
  renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
  function clear() {
    if (!model) return;
    scene.remove(model);
    const textures = new Set(), materials = new Set();
    model.traverse(object => {
      object.geometry?.dispose();
      for (const material of [].concat(object.material || [])) materials.add(material);
    });
    materials.forEach(material => {
      Object.values(material).forEach(value => { if (value?.isTexture) textures.add(value); });
      material.dispose();
    });
    textures.forEach(texture => texture.dispose()); model = null;
  }
  return { clear, async load(url) {
    const gltf = await new GLTFLoader().loadAsync(url);
    clear(); model = gltf.scene;
    const bounds = new THREE.Box3().setFromObject(model);
    const size = bounds.getSize(new THREE.Vector3());
    const extent = Math.max(size.x, size.y, size.z);
    if (!Number.isFinite(extent) || extent <= 0) { clear(); throw new Error('The generated model has no visible geometry.'); }
    const center = bounds.getCenter(new THREE.Vector3());
    model.scale.multiplyScalar(1.8 / extent);
    model.position.sub(center.multiplyScalar(1.8 / extent));
    scene.add(model); reset();
  }};
}

request('/api/health').then(health => {
  $('provider-note').textContent = 'Free Hugging Face APIs: Qwen draws the sketch; TRELLIS.2 generates both 3D models. Images are sent to these demos. Shared GPU quotas apply.';
  if (!health.generation_enabled) { $('generate').disabled = true; message('Set GENERATION_PROVIDER=huggingface on the server. Paid providers are disabled.'); }
}).catch(() => message('Upload is ready. Open this app through the FastAPI server to connect 3D generation.'));

const savedJob = new URLSearchParams(location.search).get('job');
if (savedJob) openSavedJob(savedJob);
