function mask(v: string): string { return '***'; }

export function audit(token: string): void {
  const masked = mask(token);
  console.log(masked);
}
