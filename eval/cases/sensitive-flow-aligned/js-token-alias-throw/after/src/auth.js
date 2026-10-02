function check(request) {
  const token = request.token;
  throw new Error(token);
}
