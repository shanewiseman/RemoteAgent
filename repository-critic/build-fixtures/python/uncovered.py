def denial_branch(authorized: bool) -> bool:
    if not authorized:
        return False
    return True
