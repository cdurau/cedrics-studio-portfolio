const input = document.getElementById('data-input') as HTMLInputElement;
const saveBtn = document.getElementById('save-btn') as HTMLButtonElement;
const savedDataDisplay = document.getElementById('saved-data') as HTMLParagraphElement;

function loadData() {
  const data = localStorage.getItem('app-data');
  if (data) {
    savedDataDisplay.textContent = data;
    savedDataDisplay.classList.remove('italic', 'text-gray-600');
  }
}

saveBtn.addEventListener('click', () => {
  if (input.value) {
    localStorage.setItem('app-data', input.value);
    loadData();
    input.value = '';
  }
});

loadData();
