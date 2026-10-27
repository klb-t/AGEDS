// Owner start page: record each file's lastModified as declared by this browser (stored apart
// from the server's upload times). Nothing else; the forms work without this script.
const form = document.querySelector('[data-owner-upload]');
if (form) {
  form.addEventListener('submit', () => {
    const files = form.querySelector('[data-owner-files]').files;
    const times = {};
    for (const file of files) times[file.name] = file.lastModified;
    form.querySelector('[data-owner-times]').value = JSON.stringify(times);
  });
}
