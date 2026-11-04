"""Interactive sign-in. Tokens are entered privately and never printed by us."""
from huggingface_hub import login

print("Sign in with a read-access token from https://huggingface.co/settings/tokens")
print("The token is stored by Hugging Face on this PC, outside the project.")
print("SAM 3 additionally needs approval at https://huggingface.co/facebook/sam3")
login(add_to_git_credential=False)
print("Hugging Face sign-in completed.")
