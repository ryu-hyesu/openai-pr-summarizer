from openai import OpenAI
client = OpenAI()

response = client.responses.create(
    model="gpt-4o-mini",
    input="테스트하는거임지금."
)

print(response.output_text)