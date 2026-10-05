/** Keep a surviving visible card in place when live data changes list order or height. */
export function preserveListPosition(container: HTMLElement, update: () => void): void {
  const anchors = Array.from(container.querySelectorAll<HTMLElement>("[data-packet-id]"))
    .map((node) => ({ node, top: node.getBoundingClientRect().top, bottom: node.getBoundingClientRect().bottom }))
    .filter(({ top, bottom }) => bottom > 0 && top < window.innerHeight);
  const focused = document.activeElement instanceof HTMLElement && container.contains(document.activeElement)
    ? document.activeElement : null;
  const scrollY = window.scrollY;
  update();
  if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  const anchor = anchors.find(({ node }) => node.isConnected && container.contains(node));
  const target = anchor ? window.scrollY + anchor.node.getBoundingClientRect().top - anchor.top : scrollY;
  if (window.scrollY !== target) window.scrollTo({ top: target, behavior: "instant" });
}
