def authenticate(user):
    role = user.role
    return role == 'admin'
