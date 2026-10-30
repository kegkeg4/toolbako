(() => {
  const link = document.querySelector('[data-identity-destination]');
  if (!link) return;
  const destination = new URL(link.href);
  if (destination.origin === 'https://verify.stripe.com' && !destination.username && !destination.password) {
    window.location.replace(destination.href);
  }
})();
