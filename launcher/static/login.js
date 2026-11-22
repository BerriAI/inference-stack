const form = document.querySelector('form');
const error = document.querySelector('#error');
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: form.elements.password.value}),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Check your password and try again.');
    location.replace('/' + location.hash);
  } catch (failure) {
    error.textContent = failure instanceof TypeError ? 'Could not reach the launcher. Try again.' : failure.message;
    error.hidden = false;
    button.disabled = false;
  }
});
