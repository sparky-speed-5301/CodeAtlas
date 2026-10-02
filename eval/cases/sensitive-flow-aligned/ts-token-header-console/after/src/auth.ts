export function handle(req: any): void {
  const token = req.headers.authorization;
  console.log(token);
}
