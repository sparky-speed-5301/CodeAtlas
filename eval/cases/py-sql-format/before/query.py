def find_user(connection, name):
    return connection.execute("SELECT * FROM users WHERE name = '%s'" % name).fetchone()
