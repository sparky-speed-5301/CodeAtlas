export async function load(name: string) {
  const mod = await import(name);
  return mod;
}
