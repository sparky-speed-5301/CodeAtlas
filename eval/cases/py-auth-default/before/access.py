def can_read(user, document):
    return user is None or document.owner_id == user.id
