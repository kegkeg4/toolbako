// Preserve links from earlier notifications/bookmarks without leaving the admin area.
if (location.pathname === '/admin' && !new URLSearchParams(location.search).has('section')) {
  const anchor = location.hash.slice(1);
  const panels = ['tools','users','cases','reports','finance','operation-health','readiness','audit'];
  const financeAnchors = ['payment-recovery','refund-resumption','failed-payout-recovery'];
  const section = panels.includes(anchor) ? anchor : financeAnchors.includes(anchor) ? 'finance' : '';
  if (section) location.replace(`/admin?section=${encodeURIComponent(section)}${location.hash}`);
}
