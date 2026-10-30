(() => {
  const link = document.querySelector('[data-payment-destination]');
  if (!link) return;
  const destination = new URL(link.href);
  if (['https://checkout.stripe.com', 'https://connect.stripe.com'].includes(destination.origin)
      && !destination.username && !destination.password) {
    window.location.replace(destination.href);
  }
})();
