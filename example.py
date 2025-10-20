from openai import OpenAI
client = OpenAI()

response = client.responses.create(
    model="gpt-4o-mini",
    input="테스트를 하는 게 즐거워용!"
)

print(response.output_text)