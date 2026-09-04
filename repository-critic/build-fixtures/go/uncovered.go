package example

func DenialBranch(authorized bool) bool {
	if !authorized {
		return false
	}
	return true
}
