async function load(): Promise<string> {
  return fetch("/api").text();
}
