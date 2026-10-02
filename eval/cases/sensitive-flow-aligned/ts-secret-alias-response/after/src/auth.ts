export function handle(body: any, res: any): void {
  const secret = body.secret;
  res.json({ secret });
}
