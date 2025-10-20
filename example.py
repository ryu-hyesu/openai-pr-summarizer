from openai import OpenAI
client = OpenAI()

response = client.responses.create(
    model="gpt-4o-mini",
    input="테스트 하고 있어요^^!!"
)

print(response.output_text)