// Owner start page: record each file's lastModified as declared by this browser (stored apart
// from the server's upload times), and refuse a batch above the limit before it is sent (the
// server refuses it too, from the request size). The forms work without this script.
const form = document.querySelector('[data-owner-upload]');
if (form) {
  form.addEventListener('submit', (event) => {
    const files = form.querySelector('[data-owner-files]').files;
    const limit = Number(form.dataset.ownerLimitBytes);
    const refused = form.querySelector('[data-owner-upload-refused]');
    let total = 0;
    const times = {};
    for (const file of files) {
      times[file.name] = file.lastModified;
      total += file.size;
    }
    const gib = (bytes) => (bytes / 1024 ** 3).toFixed(2);
    if (limit > 0 && total > limit) {
      event.preventDefault();
      refused.textContent = `Pliki mają razem ${gib(total)} GiB, limit paczki to ${gib(limit)} GiB. ` +
        'Wgraj je w kilku paczkach albo zmień limit w Ustawieniach.';
      refused.hidden = false;
      return;
    }
    refused.hidden = true;
    form.querySelector('[data-owner-times]').value = JSON.stringify(times);
    form.querySelector('[data-owner-uploading]').hidden = false;
    form.querySelector('button').disabled = true;
  });
}
