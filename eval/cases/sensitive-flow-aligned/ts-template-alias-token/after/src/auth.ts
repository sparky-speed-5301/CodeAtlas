export function notify(req: any): void {
  const token = req.token;
  console.log(`Token is ${token}`);
}
