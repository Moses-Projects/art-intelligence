#!/usr/bin/env python

import json
import os
import re
import requests
import sys


sys.path.append('/opt')

import moses_common.__init__ as common
import moses_common.collective
import moses_common.dynamodb
import moses_common.s3
import moses_common.secrets_manager
import moses_common.sinkinai
import moses_common.stabilityai
import moses_common.ui


ui = moses_common.ui.Interface(use_slack_format=True, usage_message="""
Generate and store images in S3 and DynamoDB.
  generate.py
  
  Options:
    -h, --help                  This help screen.
    -n, --dry_run               Run without updating anything.
    -v, --verbose               More output.
    -x, --extra_verbose         Even more output.
""")


log_level = 5
if common.get_environment() == 'dev':
	log_level = 6
limit = None
dry_run = False

secret = moses_common.secrets_manager.Secret('artintelligence.gallery/api_keys')
api_keys = secret.get_value()

collective = moses_common.collective.Collective(openai_api_key=api_keys['OPENAI_API_KEY'], log_level=log_level, dry_run=dry_run)


def handler(event, context):
	output = []
	num_of_images = event.get('count', 1)
	for i in range(num_of_images):
		success, sub_output = generate(event)
		
		if not success:
			ui.error(sub_output)
			return {
				"statusCode": 404,
				"body": sub_output
			}
		output.append(sub_output)
	
	if type(output) is list:
		output = {
			'count': len(output),
			'results': output
		}
	response = {
		"statusCode": 200,
		"body": common.make_json(output)
	}
	
#	print("response {}: {}".format(type(response), response))
	return response



def generate(event):
	print("event {}: {}".format(type(event), event))
	# Choose genre
	genre = collective.get_random_work(artist_name = event.get('artist'), genre_name = event.get('genre'))
	print("genre {}: {}".format(type(genre), genre))
	if not genre:
		return False, "Artist or genre not found"
	
	print(f"{genre.artist.name}: {genre.name}")
	
	# Get prompt
	prompt = genre.get_prompt()
	print("prompt {}: {}".format(type(prompt), prompt))
	
	# Get image config
	data = get_image(prompt, event)
	if type(data) is str:
		return data
	
	# Send to Art Intelligence bucket and db
	success = send_image(data)
	
	return True, data


def get_image(prompt, event):
	filename_prefix = None
	if 'query' in prompt and 'artist_id' in prompt['query']:
		filename_prefix = prompt['query']['artist_id']
	
	# Set save directory
	save_directory = '/tmp'
	if common.is_local():
		save_directory = os.environ.get('HOME') + '/Downloads'
	
	success = False
	model = event.get('model', 'sd3')
	data = "Model not recognized"
	
	if re.match(r'si', model) or model == 'sd3':
		# Set up stable diffusion
		stable_image = moses_common.stabilityai.StableImage(
			stability_key = api_keys['STABILITY_API_KEY'],
			model = model,
			save_directory = save_directory,
			log_level = log_level,
			dry_run = dry_run
		)
		
		# Get data for image
		data = stable_image.text_to_image(
			prompt['prompt'],
			negative_prompt=prompt.get('negative_prompt'),
# 			seed=opts['seed'],
# 			steps=opts['steps'],
# 			cfg_scale=opts['cfg'],
# 			width=prompt['width'],
# 			height=prompt['height'],
			filename_prefix=filename_prefix,
			return_args=True,
			aspect_ratio=prompt.get('aspect_ratio')
		)
		
		# Add further data
		if prompt and 'query' in prompt:
			data['query'] = prompt['query']
		
		# Generate image
		success, data = stable_image.text_to_image(data)
	
	elif re.match(r'sd', model):
		# Set up stable diffusion
		stable_diffusion = moses_common.stabilityai.StableDiffusion(
			stability_key = api_keys['STABILITY_API_KEY'],
			model = model,
			save_directory = save_directory,
			log_level = log_level,
			dry_run = dry_run
		)
		
		# Get data for image
		data = stable_diffusion.text_to_image(
			prompt['prompt'],
			negative_prompt=prompt.get('negative_prompt'),
# 			seed=opts['seed'],
# 			steps=opts['steps'],
# 			cfg_scale=opts['cfg'],
# 			width=prompt['width'],
# 			height=prompt['height'],
			filename_prefix=filename_prefix,
			return_args=True,
			aspect_ratio=prompt.get('aspect_ratio')
		)
		
		# Add further data
		if prompt and 'query' in prompt:
			data['query'] = prompt['query']
		
		# Generate image
		success, data = stable_diffusion.text_to_image(data)
	
	elif model in ['ds', 'rv']:
		# Set up sinkai
		sinkinai = moses_common.sinkinai.SinkinAI(
			sinkinai_api_key = api_keys['SINKIN_API_KEY'],
			save_directory = save_directory,
			log_level = log_level,
			dry_run = dry_run
		)
		
		# Get data for image
		data = sinkinai.text_to_image(
			prompt['prompt'],
			model=model,
			negative_prompt=prompt.get('negative_prompt'),
# 			seed=opts['seed'],
# 			steps=opts['steps'],
# 			cfg_scale=opts['cfg'],
# 			width=prompt.data['width'],
# 			height=prompt.data['height'],
			filename_prefix=filename_prefix,
			return_args=True,
			orientation=prompt.get('orientation'),
			aspect=prompt.get('aspect')
		)
		
		# Add further data
		if prompt and 'query' in prompt:
			data['query'] = prompt['query']
		
		# Generate image
		success, data = sinkinai.text_to_image(data)
	
	if not success:
		return data
	return data


def send_image(data):
	bucket_name = 'artintelligence.gallery'
	object_name = f"images/{data['filename']}"
	
# 	print("data['filepath'] {}: {}".format(type(data['filepath']), data['filepath']))
	
	bucket = moses_common.s3.Bucket(bucket_name, log_level=log_level, dry_run=dry_run)
	file = moses_common.s3.Object(bucket, object_name, log_level=log_level, dry_run=dry_run)
	response = file.upload_file(data['filepath'])
	
	del(data['filepath'])
	data['image_url'] = f"https://{bucket_name}/{object_name}"
# 	print("data['image_url'] {}: {}".format(type(data['image_url']), data['image_url']))
	flat_data = common.flatten_hash(data)
	table = moses_common.dynamodb.Table('artintelligence.gallery-images', log_level=log_level, dry_run=dry_run)
	table.put_item(flat_data)
	collective.set_images_update()
	return True



if __name__ == '__main__':
	args, opts = ui.get_options({
		"args": [ {
			"name": "arg",
			"label": "Arg"
		} ],
		"options": [ {
			"short": "a",
			"long": "artist",
			"type": "input"
		}, {
			"short": "g",
			"long": "genre",
			"type": "input"
		}, {
			"short": "l",
			"long": "limit",
			"type": "input"
		}, {
			"short": "n",
			"long": "dry_run"
		}, {
			"short": "v",
			"long": "verbose"
		}, {
			"short": "x",
			"long": "extra_verbose"
		} ]
	})
	dry_run, log_level, limit = common.set_basic_args(opts)
	collective.log_level = log_level
	
	args['artist'] = opts['artist']
	args['genre'] = opts['genre']
	
	response = handler(args, {})
	if response['statusCode'] != 200:
		ui.error(response['body'])
	elif common.is_json(response['body']):
		data = common.parse_json(response['body'])
		ui.pretty(data)
		if 'filename' in data:
			save_directory=os.environ['HOME'] + '/Downloads'
			os.system(f"open {save_directory}/{data['filename']}")
	else:
		ui.body(response['body'])

