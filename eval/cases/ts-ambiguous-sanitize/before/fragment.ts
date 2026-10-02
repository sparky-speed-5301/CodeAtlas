export function renderTrustedFragment(element: HTMLElement, fragment: string): void {
  // The contract does not state whether fragment has already been sanitized.
  element.innerHTML = fragment;
}
