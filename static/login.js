const form = document.getElementById('auth-form');
const modeInput = document.getElementById('mode');
const usernameInput = document.getElementById('username');
const passwordInput = document.getElementById('password');
const feedback = document.getElementById('username-feedback');
const messageBox = document.getElementById('message-box');
const title = document.getElementById('form-title');
const button = document.getElementById('submit-btn');
const link = document.getElementById('toggle-link');

function toggleMode(e) {
  e.preventDefault();
  if (modeInput.value === 'login') {
    modeInput.value = 'register';
    title.textContent = 'Register';
    button.textContent = 'Register';
    link.textContent = 'Login here';
    feedback.textContent = '';
    messageBox.textContent = '';
    messageBox.classList.remove('show', 'error', 'success');
  } else {
    modeInput.value = 'login';
    title.textContent = 'Login';
    button.textContent = 'Login';
    link.textContent = 'Register here';
    feedback.textContent = '';
    messageBox.textContent = '';
    messageBox.classList.remove('show', 'error', 'success');
  }
}

usernameInput.addEventListener('input', async () => {
  const username = usernameInput.value.trim();
  
  // Only check username when in register mode and username is not empty
  if (modeInput.value !== 'register' || username === '') {
    feedback.textContent = '';
    return;
  }
  
  try {
    const res = await fetch(`/check_username?username=${encodeURIComponent(username)}`);
    const data = await res.json();
    
    if (data.exists) {
      feedback.textContent = 'Username already taken.';
      feedback.style.color = 'rgba(255, 100, 100, 0.9)';
    } else {
      feedback.textContent = 'Username available.';
      feedback.style.color = 'rgba(100, 200, 100, 0.9)';
    }
  } catch (error) {
    console.error('Error checking username:', error);
    feedback.textContent = 'Error checking username.';
    feedback.style.color = 'rgba(255, 100, 100, 0.9)';
  }
});

form.addEventListener('submit', async e => {
  e.preventDefault();
  
  const username = usernameInput.value.trim();
  const password = passwordInput.value.trim();
  const mode = modeInput.value;

  // Clear previous messages
  messageBox.classList.remove('show', 'error', 'success');
  messageBox.textContent = '';

  if (!username || !password) {
    showMessage('All fields are required.', 'error');
    return;
  }

  if (mode === 'register' && password.length < 6) {
    showMessage('Password must be at least 6 characters.', 'error');
    return;
  }

  try {
    const res = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, mode })
    });

    const data = await res.json();

    if (data.success) {
      showMessage(data.message, 'success');
      setTimeout(() => {
        window.location.href = data.redirect || '/portal';
      }, 800);
    } else {
      showMessage(data.message, 'error');
    }
  } catch (error) {
    console.error('Error during authentication:', error);
    showMessage('An error occurred. Please try again.', 'error');
  }
});

function showMessage(msg, type) {
  messageBox.textContent = msg;
  messageBox.classList.add('show', type);
}