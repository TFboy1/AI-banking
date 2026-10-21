"""Local YuNet/SFace and MiniFASNet inference; no camera frames leave this process."""
from pathlib import Path
import threading

LOCK = threading.Lock()
_engine = None


class FaceCheckError(Exception):
    def __init__(self, message, security=False):
        super().__init__(message)
        self.security = security


def face_in_guide(faces, width, height):
    """Use the same centered ellipse as the camera overlay; ignore bystanders."""
    cx,cy=width*.5,height*.5
    rx,ry=width*.23,height*.36
    candidates=[]
    for face in ([] if faces is None else faces):
        x,y,bw,bh=map(float,face[:4])
        if ((x+bw/2-cx)/rx)**2+((y+bh/2-cy)/ry)**2<=1:
            candidates.append(face)
    if len(candidates)!=1:
        raise FaceCheckError('请将本人面容移到框内，框内只保留一张脸。')
    face=candidates[0];x,y,bw,bh=map(float,face[:4])
    if bh<ry or bw<rx*.65:
        raise FaceCheckError('请靠近一点，让整张脸位于框内。')
    if x<cx-rx or x+bw>cx+rx or y<cy-ry or y+bh>cy+ry:
        raise FaceCheckError('请稍微后退或调整位置，让整张脸位于框内。')
    return face


class Engine:
    def __init__(self, directory):
        import cv2
        import torch
        from .vendor.minifasnet import MiniFASNetV2, MiniFASNetV1SE
        self.path = Path(directory).resolve()
        cv2.setNumThreads(2)
        torch.set_num_threads(2)
        self.detector = cv2.FaceDetectorYN.create(str(self.path/'yunet.onnx'), '', (640,480), .9, .3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(self.path/'sface.onnx'), '')
        self.antispoof = []
        for name, factory, scale in [('2.7_80x80_MiniFASNetV2.pth', MiniFASNetV2, 2.7), ('4_0_0_80x80_MiniFASNetV1SE.pth', MiniFASNetV1SE, 4.)]:
            model = factory(conv6_kernel=(5,5))
            weights = torch.load(self.path/name, map_location='cpu', weights_only=True)
            model.load_state_dict({key.removeprefix('module.'):value for key,value in weights.items()})
            self.antispoof.append((model.eval(),scale))

    def inspect(self, data):
        from PIL import Image
        import io
        import cv2
        import numpy as np
        import torch
        try:
            with Image.open(io.BytesIO(data)) as header:
                if header.format!='JPEG' or not 160<=header.height<=720 or not 160<=header.width<=960:raise FaceCheckError('画面尺寸不正确，请重新拍摄。')
        except (OSError,ValueError) as exc:raise FaceCheckError('无法读取摄像头画面。') from exc
        image = cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
        if image is None or not 160<=image.shape[0]<=720 or not 160<=image.shape[1]<=960:
            raise FaceCheckError('请使用摄像头拍摄清晰画面。')
        h,w=image.shape[:2]
        self.detector.setInputSize((w,h))
        _, faces = self.detector.detect(image)
        face=face_in_guide(faces,w,h); x,y,bw,bh=map(float,face[:4])
        if min(bw,bh)<90 or x<0 or y<0 or x+bw>w or y+bh>h:
            raise FaceCheckError('请靠近一点，让整张脸保持在画面内。')
        gray=cv2.cvtColor(image[int(y):int(y+bh),int(x):int(x+bw)],cv2.COLOR_BGR2GRAY)
        if cv2.Laplacian(gray,cv2.CV_64F).var()<25 or not 35<float(gray.mean())<225:
            raise FaceCheckError('光线不足或画面模糊，请调整光线并保持稳定。')
        scores=[]
        for model,scale in self.antispoof:
            scale=min((h-1)/bh,(w-1)/bw,scale); nw,nh=bw*scale,bh*scale
            left,top=x+bw/2-nw/2,y+bh/2-nh/2
            left=max(0,min(left,w-1-nw));top=max(0,min(top,h-1-nh))
            crop=image[int(top):int(top+nh)+1,int(left):int(left+nw)+1]
            patch=cv2.resize(crop,(80,80)).transpose(2,0,1).copy()
            tensor=torch.from_numpy(patch).float().unsqueeze(0)/255.
            with torch.inference_mode():scores.append(float(torch.softmax(model(tensor),dim=1)[0,1]))
        if not np.isfinite(scores).all():
            raise FaceCheckError('画面检查暂未完成，请调整光线后重试。')
        aligned=self.recognizer.alignCrop(image,face)
        vector=self.recognizer.feature(aligned).reshape(-1).astype(float)
        norm=np.linalg.norm(vector)
        if not np.isfinite(vector).all() or not np.isfinite(norm) or norm<1e-8:raise FaceCheckError('无法提取稳定的人脸特征，请重新拍摄。')
        vector/=norm
        eye_distance=float(np.linalg.norm(face[4:6]-face[6:8]))
        if eye_distance<25:raise FaceCheckError('请正对镜头，稍微靠近一些。')
        if abs(float(face[5]-face[7]))/eye_distance>.22:
            raise FaceCheckError('请保持头部水平，向左右转脸，不要侧着歪头。')
        pose=float((face[8]-(face[4]+face[6])/2)/eye_distance)
        return {'vector':vector.tolist(),'pose':pose,'passive_score':sum(scores)/len(scores)}


def ready(directory):
    return all((Path(directory)/name).is_file() for name in ('yunet.onnx','sface.onnx','2.7_80x80_MiniFASNetV2.pth','4_0_0_80x80_MiniFASNetV1SE.pth'))


def inspect(data,directory):
    global _engine
    if not ready(directory):raise RuntimeError('人脸模型未安装')
    if _engine is None or _engine.path!=Path(directory).resolve():_engine=Engine(directory)
    return _engine.inspect(data)
