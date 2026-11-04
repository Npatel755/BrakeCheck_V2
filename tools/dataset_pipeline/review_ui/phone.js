/* Simplified phone controls reuse the same checked, transactional review actions. */
(() => {
  const el = (id) => document.getElementById(id);
  const small = matchMedia('(max-width: 820px), (max-width: 1180px) and (max-height: 600px) and (pointer: coarse)');
  const params = new URLSearchParams(location.search);
  function layout() {
    // Older shared links contain phone=1. A collaborator opening that link on
    // a computer still needs the desktop editor and its drawing controls.
    const phone = params.get('desktop') !== '1' && small.matches;
    document.body.classList.toggle('phone-review', phone);
    if (phone) {
      showReference(true);
      document.querySelectorAll(".comparison-heading strong")[1].textContent = "Painted lines to approve";
      for (const id of ['show-labels', 'show-mask', 'show-teacher']) el(id).checked = true;
      el('show-edges').checked = false;
      el('review-target').value = 'lanes';
      setZoom(1);
      draw();
    }
  }
  function sync() {
    const save = !el('save-correction').hidden ? el('save-correction') : el('approve');
    el('phone-approve').textContent = save.id === 'save-correction'
      ? current?.zod_audit && zodNoVisiblePaint ? 'Save empty' : 'Save'
      : current?.zod_audit && !current.geometry.lanes.length ? 'Approve no paint' : 'Approve';
    el('phone-approve').disabled = save.disabled;
    el('phone-exclude').disabled = el('reject').disabled;
    el('phone-fix').disabled = el('correct').disabled || el('correct').hidden;
    el('phone-skip').disabled = el('skip').disabled;
    el('phone-undo').hidden = el('undo').hidden;
    el('phone-undo').disabled = el('undo').disabled;
    el('phone-use-model').disabled = el('use-model').disabled || el('model-options').hidden;
    el('phone-model-name').textContent = el('model-select').selectedOptions[0]?.textContent || 'No model proposal';
    const signature = [...el('model-select').options].map(option => option.value + ':' + option.textContent).join('|');
    if (el('phone-model-select').dataset.signature !== signature) {
      el('phone-model-select').replaceChildren(...[...el('model-select').options].map(option => option.cloneNode(true)));
      el('phone-model-select').dataset.signature = signature;
    }
    el('phone-model-select').value = el('model-select').value;
    el('phone-model-select').disabled = el('model-select').disabled || !el('model-select').options.length;
    el('phone-queue').value = el('status-filter').value;
  }
  el('phone-exclude').onclick = () => el('reject').click();
  el('phone-fix').onclick = () => el('quick-fix').click();
  el('phone-approve').onclick = () => (!el('save-correction').hidden ? el('save-correction') : el('approve')).click();
  el('phone-skip').onclick = () => el('skip').click();
  el('phone-undo').onclick = () => el('undo').click();
  el('phone-use-model').onclick = () => el('use-model').click();
  el('phone-model-select').onchange = () => {
    el('model-select').value = el('phone-model-select').value;
    el('model-select').dispatchEvent(new Event('change'));
    sync();
  };
  el('phone-queue').onchange = () => {
    el('status-filter').value = el('phone-queue').value;
    el('status-filter').dispatchEvent(new Event('change'));
  };
  const observer = new MutationObserver(sync);
  for (const id of ['approve','save-correction','reject','correct','skip','undo','use-model','model-options'])
    observer.observe(el(id), {attributes:true,attributeFilter:['disabled','hidden']});
  observer.observe(el('model-select'), {childList:true,subtree:true});
  el('status-filter').addEventListener('change', sync);
  small.addEventListener('change', layout);
  layout(); sync();
})();
