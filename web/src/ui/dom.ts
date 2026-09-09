export type Child = Node | string | null | undefined;

export function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attributes: Record<string, string | boolean | EventListener | undefined> = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === false) continue;
    if (name.startsWith("on") && typeof value === "function") {
      node.addEventListener(name.slice(2).toLowerCase(), value);
    } else if (name === "className") {
      node.className = String(value);
    } else if (value === true) {
      node.setAttribute(name, "");
    } else if (typeof value === "string") {
      node.setAttribute(name, value);
    }
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

export function replaceChildren(parent: Element, ...children: Child[]): void {
  parent.replaceChildren();
  for (const child of children) {
    if (child !== null && child !== undefined) parent.append(child);
  }
}
