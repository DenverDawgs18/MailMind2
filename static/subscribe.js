    const tosModal = document.getElementById('modal-tos');
    const privacyModal = document.getElementById('modal-privacy');

    const showTosBtn = document.getElementById('show-tos');
    const closeTosBtn = document.getElementById('close-tos');

    const showPrivacyBtn = document.getElementById('show-privacy');
    const closePrivacyBtn = document.getElementById('close-privacy');

    showTosBtn.addEventListener('click', e => {
      e.preventDefault();
      tosModal.style.display = 'block';
    });

    closeTosBtn.addEventListener('click', () => {
      tosModal.style.display = 'none';
    });

    showPrivacyBtn.addEventListener('click', e => {
      e.preventDefault();
      privacyModal.style.display = 'block';
    });

    closePrivacyBtn.addEventListener('click', () => {
      privacyModal.style.display = 'none';
    });

    // Close modals if click outside content
    window.addEventListener('click', e => {
      if (e.target === tosModal) tosModal.style.display = 'none';
      if (e.target === privacyModal) privacyModal.style.display = 'none';
    });

    // Checkout form behavior
    const checkbox = document.getElementById('accept-tos');
    const checkoutButton = document.getElementById('checkout-and-portal-button');
    const form = document.getElementById('checkout-form');

    checkbox.addEventListener('change', () => {
      checkoutButton.disabled = !checkbox.checked;
    });

    form.addEventListener('submit', e => {
      if (!checkbox.checked) {
        e.preventDefault();
        alert('Please accept the Terms of Service and Privacy Policy before proceeding.');
      } else {
        // Optional: send acceptance update asynchronously
        fetch('/update_user_acceptance', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ accepted: true }),
        }).catch((err) => {
          console.error('Failed to update acceptance:', err);
        });
      }
    });