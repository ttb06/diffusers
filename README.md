# Create your story with Trấn Thành!!!
## Giới thiệu
Content cho trẻ em là "vùng đất vàng" để khai thác. Nắm bắt được tâm lý thích sự quen thuộc của trẻ nhỏ, repo này giúp bạn tạo ra ảnh dựa trên những mẫu có sẵn, ví dụ như ảnh Trấn Thành (hoặc chính bạn) đang đánh nhau với quái vật chẳng hạn:

## Bài toán
- Stable Diffusion là một mô hình sinh ảnh tốt cho các vấn đề tổng quát, tuy nhiên mô hình không thể sinh ra ảnh cho những miền tri thức hẹp
- Việc finetune lại toàn bộ mô hình rất tốn chi phí, hầu như không thể thực hiện được

## Phương pháp
Để giải quyết các vấn đề trên, em sử dụng mô hình Stable Diffusion XL cùng LoRa và DreamBooth, cụ thể:

### Stable Diffusion XL
Mô hình này tốt hơn so với mô hình Stable Diffusion ở một số điểm:
- Mạng Unet lớn hơn gấp 3 lần so với SD, kết hợp 2 text-encoder (với mô hình đã sử dụng là OpenCLIP ViT-bigG/14 và ViT-bigB/14)
- Sử dụng quá trình 2 bước: base model và refiner model, giúp tăng chất lượng cho ảnh

### LoRa
LoRa (Low-Rank Adaptation) vốn được sinh ra để finetune cho các mô hình LLM, nhưng đã được mở rộng để ứng dụng cho các mô hình Diffusion. 
Với Stable Diffusion, Cross attention được sử dụng để lấy các thông tin từ prompt vào, giúp model gen ra các ảnh như theo yêu cầu. Prompt được encode và đóng vai trò như Query, trong khi Representation của ảnh trong Latent Space sẽ là Value và Key.
Từ quan sát của (các nhà nghiên cứu)[https://arxiv.org/abs/2012.13255], weight các model thường có rank thấp. LoRa sử dụng phép phân rã ma trận (matrix decomposition) nhằm tối ưu lượng tham số cần train cho Cross attention mechanism.

### DreamBooth

## Demo

## Hướng phát triển
- MergeLoRa
- LLM
- Deploy: Do cấu hình của máy tính cá nhân không đủ mạnh, hướng đi tiếp theo của em sẽ là deploy mô hình lên server để có thể sử dụng linh hoạt hơn

## Tài liệu tham khảo
- Repo [huggingface/diffusers](https://github.com/huggingface/diffusers)
- [Stable Diffusion XL](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0)
- [LoRa](https://arxiv.org/abs/2106.09685)
- [DreamBooth](https://dreambooth.github.io/)
- [DreamBooth and LoRa for LLM, Stable Diffusion](https://huggingface.co/docs/diffusers/v0.19.3/training/lora#dreambooth)
