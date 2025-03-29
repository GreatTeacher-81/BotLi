# Set the environment variable first if not already set
# Example:
# export TOKEN_LICHESS="Your_Bot_Actual_Lichess_Token_Here"

# Checking if the environment variable is set
if [ -z "$TOKEN_LICHESS" ]; then
  echo "Error: Environment variable TOKEN_LICHESS is not set."
  exit 1
else
  # This Code Below is Gonna Replace the placeholder text on the first line of config.yml to apply the token! 
  sed -i.bak "1s#Automatically-Applyed-Here-if-U-Have-Been-Type-it-in-Environment-Variables-TOKEN_LICHESS#$TOKEN_LICHESS#" config.yml
fi
