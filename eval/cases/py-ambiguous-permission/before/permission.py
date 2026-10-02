def can_edit(user, project):
    return user.id in project.editors or user.team_id in project.teams
