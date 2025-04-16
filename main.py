from ctypes import alignment
import streamlit as st
import streamlit_pdf_viewer as st_pdf_viewer
import os
import logging
import json
import base64
import requests
import time
import urllib
import uuid
from urllib.parse import quote
import tempfile
import datetime

import google.auth.transport.requests
import google.oauth2.id_token
from google.cloud import storage
from google.cloud import pubsub_v1
from google.cloud import firestore
import pandas as pd
from google.cloud.firestore import Query

from vertexai.generative_models import GenerativeModel, Image, Part
from vertexai.preview.vision_models import ImageGenerationModel
from vertexai.preview.generative_models import GenerationConfig
from google import genai
from google.genai import types
from google.genai.types import (
    ControlReferenceConfig,
    ControlReferenceImage,
    EditImageConfig,
    Image,
    RawReferenceImage,
    StyleReferenceConfig,
    StyleReferenceImage,
    MaskReferenceImage,
    SubjectReferenceConfig,
    SubjectReferenceImage,
)
PROJECT_ID = "xxxxxxxxxxxxxxx"  # @param {type: "string", placeholder: "[your-project-id]", isTemplate: true}
if not PROJECT_ID or PROJECT_ID == "[your-project-id]":
    PROJECT_ID = str(os.environ.get("GOOGLE_CLOUD_PROJECT"))
LOCATION = os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")

try:
    # 当运行在具有 Firestore 权限的 GCE 上时，客户端库会自动查找凭据 (ADC)
    db = firestore.Client(project="yuchizh-test-449107")
    print(f"Firestore client initialized successfully using GCE ADC for project {PROJECT_ID}.")
except Exception as e:
    # 在 Streamlit UI 和控制台都显示错误
    st.error(f"Firestore 初始化失败 (使用 ADC): {e}. 请检查 GCE 实例服务账号权限和项目 ID ({PROJECT_ID})。")
    print(f"Firestore 初始化失败 (使用 ADC): {e}. 请检查 GCE 实例服务账号权限和项目 ID ({PROJECT_ID})。")
    db = None # 初始化失败则将 db 设为 None

client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
generation_model = "imagen-3.0-generate-002"
gcs_uri = "gs://xxxxxxxxxxxxxxx"

def log_api_call_to_firestore(db_client, user_id, api_type, details=None):
    """将 API 调用事件记录到 Firestore (精简版)"""
    if not db_client:
        print("Firestore client 未初始化，跳过日志记录。")
        # 不在 UI 上显示警告，保持界面简洁
        return

    # 如果 user_id 为空或无效，使用占位符
    effective_user_id = user_id if user_id else "unknown_user"

    try:
        timestamp = datetime.datetime.now(datetime.timezone.utc) # 使用 UTC 时间
        log_entry = {
            'username': effective_user_id,
            'timestamp': timestamp,
            'api_type': api_type, # API 类型，如 'text_to_image'
            # 可以选择性地添加 details，如果调用时传入了的话
            'details': details if details else {}
        }
        # 写入名为 'api_calls_log' 的集合，可修改
        db_client.collection('api_calls_log').add(log_entry)
        # 仅在控制台打印成功信息，避免干扰 UI
        print(f"Firestore log success: User={effective_user_id}, Type={api_type}")
    except Exception as e:
        # 仅在控制台打印错误信息
        print(f"Firestore log error: {e}")
        # 不在 UI 上显示错误，避免干扰用户

def encode_image(image):
    with open(image, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read())
    return encoded_string

def encode_uploaded_file(uploaded_file):
    """Encodes the content of an UploadedFile to base64."""
    if uploaded_file is not None:  # Check if a file was uploaded
        file_content = uploaded_file.read()
        encoded_string = base64.b64encode(file_content)
        return encoded_string
    else:
        return None  # Or handle the case where no file was uploaded

def __videoGenerate__(token, project_id, params):
    # Get access token calling API
    creds, project = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    access_token = creds.token

    url = f"https://us-central1-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/us-central1/publishers/google/models/veo-2.0-generate-001:predictLongRunning"
    headers = {
        'Authorization': 'Bearer ' + access_token,
        'Content-Type': 'application/json;charset=utf-8',
    }

    # 检查并处理 params 中的 bytes 类型数据 (之前的代码) ...
    if isinstance(params, bytes):
        params_str = params.decode('utf-8')
        params = json.loads(params_str)
    else:  # params 是字典
        for key, value in params.items():
            if isinstance(value, bytes):
                # 尝试解码为文本
                try:
                    params[key] = value.decode('utf-8')
                except UnicodeDecodeError:
                    # 如果不是文本，则进行 Base64 编码
                    params[key] = base64.b64encode(value).decode('utf-8')


    print("Before requests.post:")
    print(type(params))
    print(params)

    response = requests.post(url, headers=headers, json=params)
    return response

def __videoFetch__(token, project_id, params):
    creds, project = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    access_token = creds.token
    url = f"https://us-central1-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/us-central1/publishers/google/models/veo-2.0-generate-001:fetchPredictOperation"
    headers = {
        'Authorization': 'Bearer ' + access_token,
        'Content-Type': 'application/json;charset=utf-8',
    }
    response = requests.post(url,headers=headers,json=params)
    return response

def download_video_from_gcs(bucket_name, source_blob_name):
    """Downloads a blob from the bucket."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(source_blob_name)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
        blob.download_to_filename(temp_file.name)
        temp_file_path = temp_file.name

    return temp_file_path

def display_video_from_gcs(gcs_uri):
    """Displays a video from a GCS URI in Streamlit."""
    try:
        # Parse the GCS URI
        parts = gcs_uri.replace("gs://", "").split("/")
        bucket_name = parts[0]
        source_blob_name = "/".join(parts[1:])  # Reconstruct blob name

        # Download the video
        local_video_path = download_video_from_gcs(bucket_name, source_blob_name)

        # Display the video in Streamlit
        video_file = open(local_video_path, 'rb')
        video_bytes = video_file.read()
        st.video(video_bytes)
        video_file.close()

        # Clean up the temporary file
        os.remove(local_video_path)

    except Exception as e:
        st.error(f"Error displaying video: {e}")

def upload_to_gcs(uploaded_file, bucket_name, destination_blob_name=None):
    if uploaded_file is None:
        return None
    try:
        # 如果未指定 GCS 中的文件名，则生成一个唯一的文件名
        if destination_blob_name is None:
            file_extension = os.path.splitext(uploaded_file.name)[1]
            destination_blob_name = f"uploads/{uuid.uuid4()}{file_extension}"

        # 初始化 GCS 客户端
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(destination_blob_name)

        uploaded_file.seek(0) 
        blob.upload_from_file(uploaded_file, content_type=uploaded_file.type)

        print(f"File {uploaded_file.name} uploaded to gs://{bucket_name}/{destination_blob_name}")

        gcs_url = f"https://storage.googleapis.com/{bucket_name}/{quote(destination_blob_name, safe='')}"
        print(f"GCS URL: {gcs_url}")
        return gcs_url

    except Exception as e:
        print(f"Error uploading to GCS: {e}")
        return None
def get_gcs_uri_from_url(url: str) -> str:
    """Converts a GCS URL to a gs:// URI."""
    if not url.startswith("https://storage.googleapis.com/"):
        raise ValueError("Invalid GCS URL")

    # Remove the base URL
    path = url.replace("https://storage.googleapis.com/", "")

    # Decode any URL-encoded characters
    decoded_path = urllib.parse.unquote(path)

    # Split the path into bucket name and blob name
    parts = decoded_path.split("/", 1)
    if len(parts) == 1:
      bucket_name = parts[0]
      blob_name = ""
    else:
      bucket_name, blob_name = parts

    # Construct the gs:// URI
    if blob_name:
      gcs_uri = f"gs://{bucket_name}/{blob_name}"
    else:
      gcs_uri = f"gs://{bucket_name}"

    return gcs_uri

def publish_to_pubsub(project_id: str, topic_id: str, message_data: dict):
    """将消息发布到 Pub/Sub 主题。"""
    publisher = None
    try:
        # 初始化 Pub/Sub 发布者客户端 (假设 GOOGLE_APPLICATION_CREDENTIALS 已设置)
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(project_id, topic_id)

        # 数据必须是字节串 (bytestring)
        message_bytes = json.dumps(message_data).encode("utf-8")

        # 发布消息时，客户端会返回一个 future 对象
        future = publisher.publish(topic_path, message_bytes)
        # 阻塞直到消息发布完成 (可选，也可以使用回调)
        message_id = future.result()
        # print(f"已将消息 ID: {message_id} 发布到 {topic_path}") # 如果需要调试，可以保留
        return message_id
    except Exception as e:
        st.error(f"向 Pub/Sub 主题 {topic_id} 发布消息时出错: {e}")
        return None

def show_pdf_from_local_path(file_path: str):
    """从本地文件路径读取并显示PDF"""
    try:
        with open(file_path, "rb") as f:
            base64_pdf = base64.b64encode(f.read()).decode('utf-8')
        pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="700" height="1000" type="application/pdf"></iframe>'
        st.markdown(pdf_display, unsafe_allow_html=True)
    except FileNotFoundError:
        st.error(f"错误：文件未找到 - {file_path}")
    except Exception as e:
        st.error(f"加载PDF时发生错误: {e}")

def get_user_logs(db_client, user_id):
    """根据 user_id 从 Firestore 获取日志记录"""
    if not db_client:
        st.error("Firestore 客户端未初始化，无法获取记录。")
        return []
    if not user_id or "unknown" in user_id: # 不为未知用户查询
        st.warning("无法识别当前用户，无法获取记录。")
        return []

    logs = []
    try:
        # 查询用户名为 user_id 的文档，并按时间戳降序排序
        docs = db_client.collection('api_calls_log') \
                        .where('username', '==', user_id) \
                        .order_by('timestamp', direction=Query.DESCENDING) \
                        .stream()

        for doc in docs:
            log_data = doc.to_dict()
            log_data['id'] = doc.id # 添加文档 ID，虽然暂时不用但可能有用
            logs.append(log_data)
        print(f"为用户 {user_id} 从 Firestore 获取了 {len(logs)} 条记录。") # 控制台日志
    except Exception as e:
        st.error(f"查询 Firestore 记录时出错: {e}")
        print(f"查询 Firestore 记录时出错 (用户: {user_id}): {e}")
        # 提示可能需要索引
        if "index" in str(e).lower():
             st.warning("提示：Firestore 查询可能需要创建复合索引。请检查 Firestore 控制台中的错误提示或索引建议。")
    return logs

def download_image(image_bytes, filename="upscaled_image.jpg"):
    """提供下载图像的功能"""
    st.download_button(
        label=f"下载 {filename}",
        data=image_bytes,
        file_name=filename,
        mime="image/jpeg",
    )


def main():
    log_file_path = "/PATH_TO_YOUR_LOG_FILE/app.log"
    logging.basicConfig(filename=log_file_path, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    st.set_page_config(layout="wide")

    # headers = _get_websocket_headers()
    headers = st.context.headers
    user_id = ""
    if headers:
        user_id_full = headers.get("X-Goog-Authenticated-User-Email")
        if user_id_full:
            # Split the string at the colon
            parts = user_id_full.split(":")
            if len(parts) > 1:
                user_id = parts[1]  # Get the part after the colon
                logging.info(f'用户登录成功，ID为：{user_id}')
            else:
                st.write("Email format is incorrect.")
        else:
            st.write("X-Goog-Authenticated-User-Email header not found.")
            logging.warning("未找到 X-Goog-Authenticated-User-Email header")
            user_id = "unknown_no_headers"
    else:
        st.write("No HTTP headers available.")
        logging.warning("无法获取 HTTP headers")
        user_id = "unknown_no_headers"

    st.markdown("""
        <style>
        .stTextArea textarea {
            border-radius: 10px;
            border: 2px solid #0794f2;
            padding: 10px;
            background-color: #f8f9fa;
        }
        .stButton button {
            border-radius: 20px;
            background-color: #0794f2;
            color: white;
            padding: 10px 24px;
            border: none;
            box-shadow: 0 2px 5px rgba(0,0,0,0.2);
            transition: all 0.3s ease;
        }
        .stButton button:hover {
            background-color: #0794f2;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
            transform: translateY(-2px);
        }
        .image-container {
            border-radius: 15px;
            padding: 20px;
            background: #ffffff;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            margin: 10px 0;
        }
        h1 {
            color: #2E7D32;
            text-align: center;
            padding: 15px;
            margin-bottom: 20px;
            font-family: 'Arial', sans-serif;
            border-bottom: 3px solid #0794f2;
        }
        img {
            border-radius: 10px;
            transition: transform 0.3s ease;
        }
        img:hover {
            transform: scale(1.00);
        }
        .centered-title {
            text-align: left;
            width: 100%;
            margin: 0 auto;
            padding: 10px 0;
        }
        
        .centered-image {
            display: flex;
            justify-content: center;
            align-items: center;
            margin: 0 auto;
            width: 60%;  /* 可以调整这个值来控制图片宽度 */
        }
        </style>
    """, unsafe_allow_html=True)

       
    # Create sidebar with dropdown
    with st.sidebar:
        option = st.selectbox(
            'Functions',
            ('Text to Image', 'Enlarge Image', 'Edit Image', 'Image to Video', 'Text to Video', 'My Collections', 'Video Analysis', 'Batch Video Analysis')
        )
    
    left_col, right_col = st.columns([1, 9])
    
    if option == 'Text to Image':
        with left_col:
            user_prompt = st.sidebar.text_area("Type in your prompt :", height=100, key="prompt_input")
            optimize_button = st.sidebar.button("improve your prompt", help="点击以优化提示词", key="optimize_button")

            # 初始化 opt_prompt_input 和 optimize_button_clicked 在 session_state 中
            if "opt_prompt_input" not in st.session_state:
                st.session_state.opt_prompt_input = ""
            if "optimize_button_clicked" not in st.session_state:
                st.session_state.optimize_button_clicked = False

            if optimize_button:
                if not user_prompt:
                    st.warning("提示词不能为空!")
                else:
                    model = GenerativeModel("gemini-2.0-flash")

                    prompt = """
                    This word or sentense is the user_prompt that user input. It may be a character or animal or object. 
                    Please follow this style of text prompt: ‘This close-up shot of a Victoria crowned pigeon 
                    showcases its striking blue plumage and red chest. Its crest is made of delicate, lacy feathers, 
                    while its eye is a striking red color. The bird’s head is tilted slightly to the side, giving the 
                    impression of it looking regal and majestic. The background is blurred, drawing attention to the 
                    bird’s striking appearance’ ‘Animated scene features a close-up of a short fluffy monster kneeling 
                    beside a melting red candle. The art style is 3D and realistic, with a focus on lighting and texture. 
                    The mood of the painting is one of wonder and curiosity, as the monster gazes at the flame with wide 
                    eyes and open mouth. Its pose and expression convey a sense of innocence and playfulness, as if it is 
                    exploring the world around it for the first time. The use of warm colors and dramatic lighting further
                      enhances the cozy atmosphere of the image.’ ‘Drone view of waves crashing against the rugged cliffs 
                      along Big Sur’s gray point beach. The crashing blue waters create white-tipped waves, while the golden 
                      light of the setting sun illuminates the rocky shore. A small island with a lighthouse sits in the 
                      distance, and green shrubbery covers the cliff’s edge. The steep drop from the road down to the beach 
                      is a dramatic feat, with the cliff’s edges jutting out over the sea. This is a view that captures the 
                      raw beauty of the coast and the rugged landscape of the Pacific Coast Highway.’ ‘Several giant wooly mammoths 
                      approach treading through a snowy meadow, their long wooly fur lightly blows in the wind as they walk, 
                      snow covered trees and dramatic snow capped mountains in the distance, mid afternoon light with wispy clouds
                        and a sun high in the distance creates a warm glow, the low camera view is stunning capturing the large 
                        furry mammal with beautiful photography, depth of field.’‘A candid shot captures a blond 6-year-old girl 
                        strolling down a bustling city street. The warm glow of the summer sunset bathes her in golden light, 
                        casting long shadows that stretch across the pavement. The girl's hair shimmers like spun gold, her eyes 
                        sparkle with wonder as she takes in the sights and sounds around her. The blurred background of vibrant 
                        shop windows and hurrying pedestrians emphasizes her innocence and carefree spirit. The low angle of the 
                        shot adds a sense of grandeur, elevating the ordinary moment into an award-winning photograph.’ ‘A close-up 
                        shot of a man made entirely of glass riding the New York City subway. Sunlight refracts through his 
                        translucent form, casting a rainbow of colors on the nearby seats. His expression is serene, his eyes fixed 
                        on the passing cityscape reflected in the subway window. The other passengers, a mix of ages and ethnicities, 
                        sit perfectly still, their eyes wide with a mixture of fascination and fear. The carriage is silent, the only 
                        sound the rhythmic clickety-clack of the train on the tracks.’ ‘Close-up cinematic shot of a man in a crisp 
                        white suit, bathed in the warm glow of an orange neon sign. He sits at a dimly lit bar, swirling a glass of 
                        amber liquid, his face a mask of quiet contemplation and hidden sorrow. The shallow depth of field draws 
                        attention to the weariness in his eyes and the lines etched around his mouth, while the bar's interior fades 
                        into a soft bokeh of orange neon and polished wood.’ ‘This close-up shot follows a queen as she ascends the 
                        steps of a candlelit throne room. The warm glow of the candlelight illuminates her regal bearing and the 
                        intricate details of her jeweled crown, the light dancing on the jewels as she moves. She turns her head, 
                        the wisdom in her eyes and the strength in her jawline becoming more prominent. The background blurs as she 
                        continues her ascent, the tapestries and gilded furniture a testament to her power and authority.’ ‘Cinematic 
                        shot of a man dressed in a weathered green trench coat, bathed in the eerie glow of a green neon sign. 
                        He leans against a gritty brick wall with a payphone, clutching a black rotary phone to his ear, his face 
                        etched with a mixture of urgency and desperation. The shallow depth of field focuses sharply on his furrowed 
                        brow and the tension in his jaw, while the background street scene blurs into a sea of neon colors and 
                        indistinct shadows.’
                        
                        Consider these prompts. Based on these examples, rewrite the user_prompt based on the above style: 
                    """
                    print(user_prompt)
                    contents = [user_prompt, prompt]
                    responses = model.generate_content(contents)

                    # 在创建文本框之前更新 session_state
                    st.session_state.opt_prompt_input = responses.text
                    st.session_state.optimize_button_clicked = True #设置按钮点击状态为true

                    print("yuchiok0")
                    print(responses.text)

            opt_prompt_input = st.sidebar.text_area(
                "The prompt after optimized",
                height=100,
                key="opt_prompt_input",
                value=st.session_state.opt_prompt_input
            )
            number_of_images = st.sidebar.selectbox(
                'image number',
                ('1', '2', '3', '4')
            )
            img_frameOption = st.sidebar.selectbox(
                    'frame',
                    ('1:1','9:16','16:9','3:4','4:3')
                )
            
            generate_button = st.sidebar.button("Generate", key="generate_button")
                
        # Right column
        with right_col:            
            st.title("Google Imagen 3 Generates Image")
            text_to_highlight = """
            在文生图的场景里，有几点是需要注意的，以便于你能生成质量更好的图片～ \n
            提示词最佳实践可以参考：https://ai.google.dev/gemini-api/docs/imagen-prompt-guide?hl=zh-cn \n
            1. 尽量保持提示词不要太长: \n
            太长的提示词可能会让模型感到困惑 \n
            2. 提示词结构:  \n
            结构清晰，关键词、短句，保持节奏～ \n
            3. 图像风格： \n
            “时尚摄影”、“工作室拍摄”、“3D 渲染”、“卡通” \n
            4. 设定相机类型： \n
            “用数码单反相机拍摄的照片”“在佳能 EOS R5 上”
            “85mm焦距” “微距摄影” \n
            比如，试试这个： \n
            A vibrant portrait of a woman in a flowing red dress, her hair flying behind her as she sprints through a sun-drenched park, with a joyful expression on her face, captured in a dynamic, high-resolution photograph with a shallow depth of field that highlights her movement. 
            """
            st.info(text_to_highlight)
            st.markdown('<div class="centered-image">', unsafe_allow_html=True)
            # show_image = st.image("test.jpg", use_container_width =True)
            show_image_placeholder = st.empty()
            if os.path.exists("test.jpg"):
                 show_image_placeholder.image("test.jpg", use_container_width=True)
            else:
                 show_image_placeholder.markdown("*Image results will appear here*")

            if generate_button:
                if not opt_prompt_input:
                    st.warning("提示词不能为空")
                    # st.stop()
                    return

                print("Start to generate image.\n")
                logging.info("Image generating.")
                logging.info(f"用户: {user_id} 正在使用文生图，生成{number_of_images}个图片，提示词为: {opt_prompt_input}")

                try:
                    fast_images = client.models.generate_images(
                        model=generation_model,
                        prompt=opt_prompt_input,
                        config=types.GenerateImagesConfig(
                            number_of_images=number_of_images,
                            aspect_ratio=img_frameOption,
                            safety_filter_level="BLOCK_ONLY_HIGH",
                            person_generation="ALLOW_ADULT",
                        ),
                    )
                    print('type={}'.format(type(fast_images)))
                                             
                    # Imagen 3 Fast image generation
                    # cv2.imwrite("generated_image.jpg", fast_image.generated_images[0].image._pil_image)
                    if fast_images is not None:
                        show_image_placeholder.empty()
                        for idx, image in enumerate(fast_images.generated_images):
                            st.write(f"第{idx + 1}张图片 ⬇️ ")    
                            st.image(image.image._pil_image)
                            log_details = {"prompt": opt_prompt_input}
                            log_api_call_to_firestore(db, user_id, "text_to_image", details=log_details)
                    
                except (TypeError, KeyError, requests.exceptions.RequestException) as e:
                    logging.error(f"Error during image display: {e}")
                    logging.error(f"用户: {user_id} 正在使用文生图，提示词被和谐，提示词为: {opt_prompt_input}")
                    st.error("内容被河蟹啦。。请更换提示词")
                
                
    elif option == 'Enlarge Image':
        with left_col:
            with left_col:
                # uploaded_file = st.sidebar.file_uploader("upload the image!", type=["jpg", "jpeg", "png"])

                uploaded_files = st.sidebar.file_uploader(
                    "选择图片文件",
                    type=["jpg", "jpeg", "png"], 
                    accept_multiple_files=True 
                )

            with st.sidebar:
                upscale_factor = st.selectbox(
                    'upscale factor',
                    ('x2', 'x4')
                )
                
            upscale_button = st.sidebar.button("Upscale the Image")
                
        # Right column
        with right_col:            
            st.title("Google Imagen 3 Generates Image")
            text_to_highlight = """
            您可以使用 Imagen on Vertex AI 的放大功能来增加图片的大小，而不会降低质量。\n
            UPSCALE_FACTOR 是图片的放大系数。如果未指定，系统将根据输入图片的较长边和 sampleImageSize 确定放大系数。可用值：x2 或 x4。\n
            """
            st.info(text_to_highlight)
            st.markdown('<div class="centered-image">', unsafe_allow_html=True)
            # show_image = st.image("test.jpg", use_container_width =True)
            show_image_placeholder = st.empty()
            if os.path.exists("test.jpg"):
                 show_image_placeholder.image("test.jpg", use_container_width=True)
            else:
                 show_image_placeholder.markdown("*Image results will appear here*")

            if upscale_button:
                logging.info("Image upscaling.")
                logging.info(f"用户: {user_id} 正在使用图片超分，超分参数为: {upscale_factor}")
                show_image_placeholder.empty()
                st.info(f"正在处理 {len(uploaded_files)} 个文件🏃") # 提示信息

                # 进度条
                progress_bar = st.progress(0)

                for i, uploaded_file in enumerate(uploaded_files):
                    try:
                        image_data = uploaded_file.read()
                        image = types.Image(
                                    image_bytes=image_data,
                                    mime_type="image/png",
                                )
                        
                        response = client.models.upscale_image(
                            model='imagen-3.0-generate-002',
                            image=image,
                            upscale_factor=upscale_factor,
                            config=types.UpscaleImageConfig(
                                include_rai_reason=True,
                                output_mime_type='image/jpeg',
                            ),
                        )

                        log_details = {"upscale_factor": upscale_factor}
                        if response is not None:
                            generated_image = response.generated_images[0]
                           
                            print(f"Type of generated_image.image: {type(generated_image.image)}")
                            print(f"Attributes of generated_image.image: {dir(generated_image.image)}")
                            if hasattr(generated_image.image, 'image_bytes'):
                                image_bytes = generated_image.image.image_bytes
                                filename = f"upscaled_image_{i+1}.jpg"
                                download_image(image_bytes, filename)
                                log_api_call_to_firestore(db, user_id, "image_upscale", details=log_details)
                            
                            # st.write(f"第{i + 1}张图片 ⬇️ ") 
                            # image_bytes = response.generated_images[0].image_bytes
                            # filename = f"upscaled_image_{i+1}.jpg"
                            # download_image(image_bytes, filename)
                            # st.image(response.generated_images[0].image._pil_image)
                            # log_api_call_to_firestore(db, user_id, "image_upscale", details=log_details)

                    except (TypeError, KeyError, requests.exceptions.RequestException) as e:
                        logging.error(f"Error during image display: {e}")
                        logging.error(f"用户: {user_id} 正在使用图片超分，被和谐")
                        st.error(f"第{i}张图片内容被河蟹啦。。")
                    
                    progress_bar.progress((i + 1) / len(uploaded_files))
                    time.sleep(0.1)                     

    elif option == 'Edit Image':
        with left_col:
            with left_col:
                uploaded_file = st.sidebar.file_uploader("upload the image!", type=["jpg", "jpeg", "png"])

            with st.sidebar:
                person_generation = st.sidebar.selectbox(
                    'person generation options',
                    ('allow_adult', 'dont_allow')
                )
                enhance_prompt = st.sidebar.selectbox(
                    'Need Prompt enhancement?',
                    ('True', 'False')
                )
                number_of_images = st.sidebar.selectbox(
                    'image number',
                    ('1', '2', '3', '4')
                )

                subject_type = st.sidebar.selectbox(
                    'subject type',
                    ('SUBJECT_TYPE_DEFAULT', 'SUBJECT_TYPE_PERSON', 'SUBJECT_TYPE_ANIMAL', 'SUBJECT_TYPE_PRODUCT')
                )
                
                control_type = st.sidebar.selectbox(
                    'Control type',
                    ('CONTROL_TYPE_SCRIBBLE', 'CONTROL_TYPE_FACE_MESH', 'CONTROL_TYPE_CANNY')
                )

                subject_description = st.sidebar.text_area("Descript the image you uploaded", height=100, key="descriptions")
                edit_prompt = st.sidebar.text_area("Text in the edit prompts", height=100, key="edit_prompt")
                edit_generate_button = st.sidebar.button("Edit the Image")
                
        # Right column
        with right_col:            
            st.title("Google Imagen 3 Generates Image")
            text_to_highlight = """
            图生图的提示词有一些技巧 \n
            如果你想生成同类产品，那么你先上传一张照片，然后可以使用提示词模版，切记引用的图片要用 [1] 标出： \n
            Create an image about SUBJECT_DESCRIPTION [1] to match the description: ${PROMPT} \n
            例如：Create an image about Luxe Elixir hair oil, golden liquid in glass bottle [1] to match the description: A close-up, high-key image of a woman's hand holding Luxe Elixir hair oil, golden liquid in glass bottle [1] against a pure white background. The woman's hand is well-lit and the focus is sharp on the bottle, with a shallow depth of field blurring the background and emphasizing the product. The lighting is soft and diffused, creating a subtle glow around the bottle and hand. The overall composition is simple and elegant, highlighting the product's luxurious appeal. \n
            \n
            如果你想生成相同画风的人，先上传一张照片，然后可以使用提示词模版，切记引用的图片要用 [1] 标出：\n
            Generate an image of SUBJECT_DESCRIPTION [1]... \n
            例如：Generate an image of the girl [1] with a happy expression, looking directly at the camera. Her head should be tilted slightly to the right, and her hair should be styled in a way that is... \n
            \n
            如果你想生成相同的人物，先上传一张照片，然后可以使用提示词模版，切记引用的图片要用 [1] 标出：\n
            Generate an image of SUBJECT_DESCRIPTION [1] with the facemesh from the control image [2]. ${PROMPT} \n
            例如：Generate an image of the person [1] with the facemesh from the control image [2]. The person should be looking straight ahead with a neutral expression. The background should be a ... \n
            """
            st.info(text_to_highlight)

            col1, col2 = st.columns(2)

            with col1:
                origin_image = st.image("test.jpg", caption="Original Image", use_container_width=True)
            with col2:
                edit_image = st.image("test.jpg", use_container_width=True)

            st.markdown('<div class="centered-image">', unsafe_allow_html=True)
 
            if edit_generate_button:
                origin_image.image(uploaded_file)
                if not edit_prompt:
                    st.warning("提示词不能为空")
                    # st.stop()
                    return

                print("Start to generate image.\n")
                logging.info("Image generating.")
                logging.info(f"用户: {user_id} 正在使用图生图，生成个图片，提示词为: {edit_prompt}")

                try:
                    image_data = uploaded_file.read()
                    image = types.Image(
                                image_bytes=image_data,
                                mime_type="image/png",
                            )

                    subject_reference_image = SubjectReferenceImage(
                        reference_id=1,
                        reference_image=image,
                        config=SubjectReferenceConfig(
                            subject_description=subject_description, 
                            subject_type=subject_type
                        ),
                    )
                    control_reference_image = ControlReferenceImage(
                        reference_id=2,
                        reference_image=image,
                        config=ControlReferenceConfig(control_type=control_type),
                    )

                    response = client.models.edit_image(
                        model='imagen-3.0-capability-001',
                        prompt=edit_prompt,
                        reference_images=[subject_reference_image, control_reference_image],
                        config=EditImageConfig(
                            edit_mode="EDIT_MODE_DEFAULT",
                            number_of_images=number_of_images,
                            seed=1,
                            safety_filter_level="BLOCK_ONLY_HIGH",
                            person_generation=person_generation,
                        ),
                    )

            
                    if response.generated_images:
                        edit_image.empty()
                        generated_image_object = response.generated_images[0]

                        image_displayed = False
                        # 主要检查路径：generated_image_object.image.image_bytes
                        if hasattr(generated_image_object, 'image'):
                            image_container = generated_image_object.image
                            if hasattr(image_container, 'image_bytes'):
                                image_data = image_container.image_bytes
                                # 尝试获取 mime_type
                                mime_type = getattr(image_container, 'mime_type', getattr(generated_image_object, 'mime_type', 'image/png'))
                                st.image(image_data, caption=f"Edited Image (MIME: {mime_type})")
                                image_displayed = True
                            elif hasattr(image_container, 'gcs_uri'):
                                image_uri = image_container.gcs_uri
                                mime_type = getattr(image_container, 'mime_type', getattr(generated_image_object, 'mime_type', 'N/A'))
                                st.warning(f"API returned GCS URI: {image_uri}")
                                st.image(image_uri, caption=f"Edited Image (from GCS URI, MIME: {mime_type})")
                                # !!! 如果 st.image 无法加载 GCS URI，需要添加 GCS 下载代码 !!!
                                image_displayed = True

                        # 备用检查路径 (如果 .image 不存在或内部没有数据)
                        if not image_displayed:
                            if hasattr(generated_image_object, 'image_uri'):
                                image_uri = generated_image_object.image_uri
                                mime_type = getattr(generated_image_object, 'mime_type', 'N/A')
                                st.warning(f"API returned GCS URI directly: {image_uri}")
                                st.image(image_uri, caption=f"Edited Image (from GCS URI, MIME: {mime_type})")
                                # !!! 如果 st.image 无法加载 GCS URI，需要添加 GCS 下载代码 !!!
                                image_displayed = True

                        # 如果所有路径都失败
                        if not image_displayed:
                            st.error("Failed to find usable image data (bytes or URI) in the response object.")
                            st.write("GeneratedImage Object Details:", generated_image_object)

                    else:
                        st.error("内容被河蟹啦。。请更换提示词。。请更换提示词 或图片")
                        # 可选：打印完整响应进行调试
                        # st.write(response)
                except (TypeError, KeyError, requests.exceptions.RequestException) as e:
                    st.error(f"Error during image display: {e}")
                    st.error("内容被河蟹啦。。请更换提示词")

    elif option == 'Image to Video':  # Edit Image mode
        # Left column
        with left_col:

            uploaded_file = st.sidebar.file_uploader("upload the image!", type=["jpg", "jpeg", "png"])

            with st.sidebar:
                frameOption = st.selectbox(
                    'frame',
                    ('16:9', '9:16')
                )
                fpsOption = st.sidebar.selectbox(
                    'fps',
                    ('24', '30')
                )
                duration_seconds = st.sidebar.selectbox(
                    'duration seconds',
                    ('5', '6', '7', '8')
                )
                person_generation = st.sidebar.selectbox(
                    'person generation options',
                    ('allow_adult', 'dont_allow')
                )
                enhance_prompt = st.sidebar.selectbox(
                    'Need Prompt enhancement?',
                    ('True', 'False')
                )
                number_of_videos = st.sidebar.selectbox(
                    'video number',
                    ('1', '2', '3', '4')
                )
            edit_opt_prompt_input = st.sidebar.text_area("Text in the prompts", height=100, key="edit_opt_prompt_input")
            edit_generate_button = st.sidebar.button("Generate Videos")
                
        # Right column
        with right_col:
            st.header("Google Veo 2 enable image to Video")
            text_to_highlight = "当从图像生成视频时，建议您提供一个简单的文本提示来描述您想要看到的动作。输入在页面左下角的对话框里（最多 10 个词）"
            st.info(text_to_highlight)

            col1, col2 = st.columns(2)
            with col1:
                origin_image = st.image("test.jpg", caption="Original Image", use_container_width=True)
            with col2:
                edit_image = st.image("test.jpg", caption="Video", use_container_width=True)

            if edit_generate_button:
                if uploaded_file is not None:
                    image_data = uploaded_file.read()
                    # 显示上传的原始图片文件
                    logging.info(f'用户: {user_id} 上传图片，uploaded_file={uploaded_file.name}')
                    print('uploaded_file={}'.format(uploaded_file))
                    origin_image.image(uploaded_file)
                    output_gcs = "gs://xxxxxxxxxxxxxxx"  # @param {type: 'string'}
                    operation = None
                    try:
                        operation = client.models.generate_videos(
                            model="veo-2.0-generate-001",
                            prompt=edit_opt_prompt_input,
                            image=types.Image(
                                image_bytes=image_data,
                                mime_type="image/png",
                            ),
                            config=types.GenerateVideosConfig(
                                aspect_ratio=frameOption,
                                output_gcs_uri=output_gcs,
                                number_of_videos=number_of_videos,
                                duration_seconds=duration_seconds,
                                person_generation=person_generation,
                                enhance_prompt=enhance_prompt,
                                fps=fpsOption,
                            ),
                        )
                        logging.info(f"用户: {user_id} 正在使用图生视频，生成{number_of_videos}个视频，提示词为: {edit_opt_prompt_input}")

                    except Exception as submit_error:
                         st.error(f"提交图生视频任务失败: {submit_error}")
                         logging.error(f"用户: {user_id} 图生视频任务提交失败。错误: {submit_error}", exc_info=True)
                         # 可选：记录提交失败到 Firestore
                         # log_details = {"error": str(submit_error), "status": "submission_failed"}
                         # log_api_call_to_firestore(db, user_id, "image_to_video_submit", details=log_details)
                         operation = None # 确保 operation 为 None，跳过轮询

                    if operation:    
                        while not operation.done:
                            time.sleep(15)
                            operation = client.operations.get(operation)
                            logging.info(f"Operation status: {operation}")
                            print(operation)

                        if operation and operation.response:
                            try:
                                logging.info("Video generating.")
                                edit_image.empty()
                                for video_object in operation.result.generated_videos:
                                    gcs_uri = video_object.video.uri
                                    display_video_from_gcs(gcs_uri)
        
                                    # 获取操作名称用于日志记录 (如果可用)
                                    op_name = "N/A"
                                    if hasattr(operation, '_operation') and hasattr(operation._operation, 'name'):
                                        op_name = operation._operation.name
                                    elif hasattr(operation, 'operation') and hasattr(operation.operation, 'name'):
                                        op_name = operation.operation.name

                                    log_details = {
                                        "prompt": edit_opt_prompt_input,
                                        "num_videos": number_of_videos,
                                        "duration": duration_seconds,
                                        "image_name": uploaded_file.name,
                                        "operation_name": op_name, # 记录操作名
                                        "GCS_location": gcs_uri
                                    }
                                    log_api_call_to_firestore(db, user_id, "image_to_video_submit", details=log_details) # 记录提交事件
                                # show_image.video(operation.result.generated_videos[0].video.uri)
                                logging.info(f"用户: {user_id} 视频生成成功")

                            except (TypeError, KeyError, requests.exceptions.RequestException) as e:
                                logging.error(f"Error during video display: {e}")
                                st.error("内容被河蟹啦。。")
                    
                        elif operation and operation.error: # 检查操作是否包含错误信息
                             logging.error(f"Operation failed: {operation.error.message}")
                             st.error(f"视频生成操作失败: {operation.error.message}")
                             # 可选：记录操作失败
                             # log_details = {"operation_name": op_name, "error": operation.error.message, "status": "failed"}
                             # log_api_call_to_firestore(db, user_id, "image_to_video_result", details=log_details)
        
                        else:
                            logging.error(f"Operation failed, operation.response is none, operation: {operation}")
                            if operation: # 如果 operation 存在但 response/error 都为空
                                st.error("视频生成操作完成，但状态未知或无有效响应。")
                                
    elif option == 'Text to Video':
        with left_col:
            with st.sidebar:
                aspect_ratio = st.selectbox(
                    'frame',
                    ('16:9', '9:16')
                )
                fpsOption = st.sidebar.selectbox(
                    'fps',
                    ('24', '30')
                )
                number_of_videos = st.sidebar.selectbox(
                    'video number',
                    ('1', '2', '3', '4')
                )
                duration_seconds = st.sidebar.selectbox(
                    'duration seconds',
                    ('5', '6', '7', '8')
                )
                person_generation = st.sidebar.selectbox(
                    'person generation options',
                    ('allow_adult', 'dont_allow')
                )
                enhance_prompt = st.sidebar.selectbox(
                    'Need Prompt enhancement?',
                    ('True', 'False')
                )
            opt_prompt_input = st.sidebar.text_area("The prompt after optimized", height=100, key="opt_prompt_input")
            generate_button = st.sidebar.button("Generate", key="generate_button")
                
        # Right column
        with right_col:
            st.title("Text to Video by Google Veo 2")
            text_to_highlight = """
            文本生视频的提示 应该比图像到视频更详细，使用正确的关键字实现更好的控制。
            我们已确定了一些与 Veo 配合良好的关键字列表，请在您的人工书面提示中使用这些关键词来获得所需的相机动作或风格
            比如：\n
            Subject(物体): Who or what is the main focus of the shot e.g. happy woman in her 30s \n
            Scene（场景）: Where is the location of the shot (on a busy street, in space) \n
            Action（动作）: What is the subject doing (walking, running, turning head) \n
            Camera Motion（摄像轨迹）: What the camera is doing e.g. POV shot, Aerial View, Tracking Drone view, Tracking Shot \n
            试试这个：“A cute creatures with snow leopard-like fur is walking in winter forest, 3D cartoon style render \n
            或者： An architectural rendering of a white concrete apartment building with flowing organic shapes, seamlessly blending with lush greenery and futuristic elements.
            """
            st.info(text_to_highlight)
            st.markdown('<div class="centered-image">', unsafe_allow_html=True)
            show_image = st.image("test.jpg", use_container_width =True)
            if generate_button:
                if not opt_prompt_input:
                    st.warning("提示词不能为空")
                    return

                # aspect_ratio = "16:9"  # @param ["16:9", "9:16"]
                output_gcs = "gs://xxxxxxxxxxxxxxx"  # @param {type: 'string'}

                operation = client.models.generate_videos(
                    model="veo-2.0-generate-001",
                    prompt=opt_prompt_input,
                    config=types.GenerateVideosConfig(
                        aspect_ratio=aspect_ratio,
                        output_gcs_uri=output_gcs,
                        number_of_videos=number_of_videos,
                        duration_seconds=duration_seconds,
                        person_generation=person_generation,
                        enhance_prompt=enhance_prompt,
                    ),
                )
                logging.info(f"用户: {user_id} 正在使用文生视频，生成{number_of_videos}个视频，提示词为: {opt_prompt_input}")

                while not operation.done:
                    time.sleep(15)
                    operation = client.operations.get(operation)
                    logging.info(f"Operation status: {operation}")
                    print(operation)

                if operation.response:
                    try:
                        logging.info("Video generating.")
                        show_image.empty()
                        if len(operation.result.generated_videos) > 0:
                            for video_object in operation.result.generated_videos:
                                gcs_uri = video_object.video.uri
                                display_video_from_gcs(gcs_uri)
                                # 获取操作名称用于日志记录 (如果可用)
                                op_name = "N/A"
                                if hasattr(operation, '_operation') and hasattr(operation._operation, 'name'):
                                    op_name = operation._operation.name
                                elif hasattr(operation, 'operation') and hasattr(operation.operation, 'name'):
                                    op_name = operation.operation.name

                                log_details = {
                                    "prompt": opt_prompt_input,
                                    "duration": duration_seconds,
                                    "operation_name": op_name, # 记录操作名
                                    "GCS_location": gcs_uri
                                }
                                log_api_call_to_firestore(db, user_id, "text_to_video_submit", details=log_details) # 记录提交事件
                            # show_image.video(operation.result.generated_videos[0].video.uri)
                            logging.info(f"用户: {user_id} 视频生成成功")
                        else:
                            logging.error(f"Error during video display: {e}")
                            st.error("内容被河蟹啦。。")
                    except (TypeError, KeyError, requests.exceptions.RequestException) as e:
                        logging.error(f"Error during video display: {e}")
                        st.error("内容被河蟹啦。。")
                       
                else:
                    logging.error(f"Operation failed, operation.response is none, operation: {operation}")
        
    elif option == 'My Collections': # 使用英文名匹配上面 selectbox 中的值
        with right_col: # 在右侧主区域显示
            st.title(f"📜 {user_id} 的调用记录") # 显示包含用户名的标题

            if not user_id or "unknown" in user_id:
                st.warning("无法识别当前用户，无法查询记录。")
            elif not db:
                 st.error("数据库连接失败，无法查询记录。")
            else:
                # 获取该用户的日志记录
                user_logs = get_user_logs(db, user_id)

                if not user_logs:
                    st.info("未找到您的调用记录。")
                else:
                    processed_data = []
                    for log in user_logs:
                        api_type = log.get('api_type', '未知类型')
                        details = log.get('details', {})
                        prompt = details.get('prompt', 'N/A') # 获取提示词，如果不存在则为 'N/A'
                        timestamp = log.get('timestamp', None) # 获取时间戳
                        cost = 0.0
                        GCS_location = details.get('GCS_location', None)

                        parts = str(GCS_location).replace("gs://", "").split("/")
                        bucket_name = parts[0]
                        source_blob_name = "/".join(parts[1:])
                        gcs_url = f"https://storage.mtls.cloud.google.com/{bucket_name}/{source_blob_name}"
                        
                        # 确定操作名称
                        operation_name = api_type # 默认为 api_type
                        if api_type == 'text_to_image':
                            operation_name = "文本生成图片"
                            cost = 1.0
                            processed_data.append({
                                "操作名称": operation_name,
                                "用户提示词": prompt,
                                "时间": timestamp.strftime('%Y-%m-%d %H:%M:%S') if timestamp else "N/A", # 格式化时间戳
                                "视频路径": "None"
                            })
                        elif api_type in ['image_to_video_submit', 'text_to_video_submit']:
                            # 从 details 中获取视频时长
                            try:
                                duration = float(details.get('duration', 0)) # 尝试转为浮点数，默认为 0
                            except (ValueError, TypeError):
                                duration = 0 # 如果转换失败，时长计为 0
                                print(f"警告: 在日志 {log.get('id','N/A')} 中未能解析 duration_seconds: {details.get('duration_seconds')}")

                            cost = duration * 0.5
                            operation_name = "生成视频" if api_type == 'image_to_video_submit' else "文本生成视频"
                        # 可以为其他 api_type 添加更多规则

                            processed_data.append({
                                "操作名称": operation_name,
                                "用户提示词": prompt,
                                "费用 (Cost)": cost,
                                "时间": timestamp.strftime('%Y-%m-%d %H:%M:%S') if timestamp else "N/A", # 格式化时间戳
                                "视频路径": gcs_url
                            })

                    # 创建 Pandas DataFrame
                    df = pd.DataFrame(processed_data)

                    # 选择并重排要显示的列
                    display_df = df[["操作名称", "用户提示词", "时间", "视频路径"]] 

                    # 显示 DataFrame
                    st.dataframe(display_df, use_container_width=True)
                    # 可选：计算并显示总费用
                    # total_cost = df["费用 (Cost)"].sum()
                    # st.metric("Total Estimated Cost", f"${total_cost:.2f}")
                    # st.write("实际费用以console账单为主")
                   

    else:
        st.title("TBD")        

if __name__ == "__main__":
    main()
   


