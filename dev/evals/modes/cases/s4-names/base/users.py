USERS = {}


def add_user(user_id, name):
    USERS[user_id] = {"name": name}


def rename_user(user_id, new_name):
    USERS[user_id]["name"] = new_name
