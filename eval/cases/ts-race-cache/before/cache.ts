let cached: string | undefined;

export async function getValue(load: () => Promise<string>): Promise<string> {
  if (!cached) cached = await load();
  return cached;
}
