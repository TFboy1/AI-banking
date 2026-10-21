// Keep the chat composer above the mobile keyboard, including Safari's visual viewport.
const viewport = window.visualViewport;
const update = () => {
  const mobile = window.matchMedia('(max-width: 760px)').matches;
  document.documentElement.style.setProperty('--chat-viewport-height', mobile ? `${viewport?.height ?? window.innerHeight}px` : '100dvh');
};
viewport?.addEventListener('resize', update);
window.addEventListener('resize', update);
update();
