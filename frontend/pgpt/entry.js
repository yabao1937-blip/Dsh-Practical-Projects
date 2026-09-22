// Navigation only: the existing DS/GPT calculations and state are untouched.
window.addEventListener('load', () => {
    const match = /^#pgpt-return-(ds|gpt)$/.exec(location.hash);
    if (!match) return;
    document.querySelector('[data-page="page-coarse"]')?.click();
    CoarsePage.switchEngine(match[1]);
    history.replaceState(null, '', location.pathname + location.search);
});
