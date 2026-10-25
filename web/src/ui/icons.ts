/**
 * Compact inline SVG icons for icon-only controls. Unicode glyph labels render
 * unreliably across Telegram clients, so icon buttons carry drawn shapes with
 * localized accessible names instead of font-dependent symbols.
 */

const ICON_PATHS = {
  // Trash bin: lid with handle plus the slatted can body.
  trash: "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z",
  // Repeat arrow: a circular arrow pointing counter-clockwise.
  repeat: "M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z",
} as const;

export type IconName = keyof typeof ICON_PATHS;

export function icon(name: IconName): SVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon icon-${name}`);
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICON_PATHS[name]);
  svg.append(path);
  return svg;
}
