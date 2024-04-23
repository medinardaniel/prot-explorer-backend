from flask import Flask, request, jsonify
from pymongo import MongoClient
import os
import json
from dotenv import load_dotenv
from flask_cors import CORS, cross_origin
import requests
import onnxruntime as ort
import numpy as np
import time
import boto3

load_dotenv()

app = Flask(__name__)

origins = [
    "http://localhost:3000",
    "https://prot-explorer-frontend.vercel.app/",
    "https://prot-explorer-frontend-daniels-projects-a44d4a0e.vercel.app/",
    "https://prot-explorer-frontend-git-main-daniels-projects-a44d4a0e.vercel.app/",
    "https://proteinexplorer.vercel.app/"
]

CORS(app, resources={r"/process": {"origins": origins}})

# Environment variables (Normally these should be securely stored)
MONGODB_URI = os.getenv("MONGODB_URI")
EMBEDDINGS_API_URL = os.getenv("EMBEDDINGS_API_URL")
EMBEDDINGS_API_KEY = os.getenv("EMBEDDINGS_API_KEY")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION")

s3 = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)

def download_file_from_s3(bucket_name, object_key, local_file_path):
    s3.download_file(Bucket=bucket_name, Key=object_key, Filename=local_file_path)

# Specify your S3 bucket and object key
bucket_name = 'prot-explorer'
object_key = 'model.onnx'
local_model_path = 'model.onnx'

# Download the model file
download_file_from_s3(bucket_name, object_key, local_model_path)

session = ort.InferenceSession(local_model_path, providers=['CPUExecutionProvider'])

def generate_tags(embedding):
    """
    Use the loaded model to generate tags for the input string.
    :param embedding: 1x1024 tensor Gist embedding of the input string.
    :return: A list of tags generated by the model.
    """
    # Convert embedding to numpy
    input_data = np.array(embedding, dtype=np.float32)
    input_tensor = input_data.reshape(1, 1024)

    # Prepare the input dictionary
    input_name = session.get_inputs()[0].name
    input_dict = {input_name: input_tensor}

    # Run the model
    output = session.run(None, input_dict)

    # Round each prediction in output[0]
    rounded_predictions = output[0][0].round()

    with open('reverse_vocab.json', 'r') as f:
        reverse_vocab = json.load(f)

    tags = [reverse_vocab[str(i)] for i, value in enumerate(rounded_predictions) if value == 1]

    return tags

def embed_func_description(func_description):
    """
    Sends a request to the specified Hugging Face model API and returns the response.
    :param payload: The data to send in the request.
    :return: The JSON response from the API.
    """
    payload = {
        "inputs": func_description,
        "parameters": {}
    }
    emb_headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + EMBEDDINGS_API_KEY,
        "Content-Type": "application/json"
    }

    for _ in range(30):  # Try 30 times (5 minutes)
        response = requests.post(EMBEDDINGS_API_URL, headers=emb_headers, json=payload)
        response_json = response.json()

        # If the response is not an error, return the embeddings
        if response_json != {'error': '503 Service Unavailable'}:
            return response_json['embeddings']

        # If the response is an error, sleep for 30 seconds before trying again
        time.sleep(10)

    # If all 4 attempts failed, return None or raise an exception
    return None

def get_similar_proteins(embedding, number_value, explore_unique_entry_ids):
    # Connect to your MongoDB database
    client = MongoClient(MONGODB_URI)
    db = client['proteinExplorer']
    collection = db['protein_embeddings']
    
    # Perform your query to find similar vectors
    query = [
        {
            "$vectorSearch": {
                "index": "func_embeddings_index",
                "path": "func_embedding",
                "queryVector": embedding,
                "numCandidates": number_value*500,
                "limit": number_value*100
            }
        }
    ]
    query_results = list(collection.aggregate(query))
    
    # Process query results
    results = []
    seen_entry_ids = set()
    for result in query_results:
        if explore_unique_entry_ids:
            # Ensure unique entry_id
            if result['entry_id'] not in seen_entry_ids:
                seen_entry_ids.add(result['entry_id'])
                results.append(result)
                if len(results) == number_value:
                    break
        else:
            results.append(result)
            if len(results) == number_value:
                break
    
    # Simplify the results before sending them back
    simplified_results = [{
        'entry_id': result['entry_id'],
        'entity_id': result['entity_id'],
        'name': result['name'],
        'function_shortened': result['function_shortened'],
        'mondo_annotations': result['mondo_names']
    } for result in results]
    
    return simplified_results

@app.route('/process_input', methods=['POST'])
@cross_origin()
def process_input():
    data = request.json
    func_description = data.get('function')
    number_value = data.get('number')
    explore_unique_entry_ids = data.get('exploreUniqueEntryIds')

    embedding = embed_func_description(func_description)
    if embedding is None:
        return jsonify({'error': 'Failed to get embeddings from the API.'})
    
    # Classify input
    tags = generate_tags(embedding)
    
    # Read database and get results
    similar_proteins = get_similar_proteins(embedding, number_value, explore_unique_entry_ids)

    # Create a dictionary to send as the response
    response = {
        'tags': tags,
        'similar_proteins': similar_proteins
    }

    print('Similar proteins length:', len(similar_proteins))
    
    return jsonify(response)

if __name__ == '__main__':
    app.run(debug=True)
