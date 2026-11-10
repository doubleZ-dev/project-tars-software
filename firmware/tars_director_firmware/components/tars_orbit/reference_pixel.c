#include "reference_pixel.h"

static const int8_t wakeup[96][4] = {
#include "reference_wakeup.def"
};
static const uint16_t normal[]={0x0FE,0x1FF,0x1FF,0x0FE,0x07C};
static const uint16_t smile[]={0,0x07C,0x0C6,0x183,0};
static const uint16_t closed[]={0,0x183,0x0C6,0x07C,0};
static const uint16_t half[]={0,0,0x1FF,0x0FE,0x07C};
static const uint16_t angry[]={0x1E0,0x1FC,0x1FF,0x0FE,0x07C};
static const uint16_t sad[]={0x00F,0x03F,0x1FF,0x0FE,0x07C};
static const uint16_t round_eye[]={0x07C,0x0FE,0x1FF,0x1FF,0x1FF,0x0FE,0x07C};
static const uint16_t wide_eye[]={0x07C,0x0FE,0x1FF,0x1FF,0x0FE,0x07C};
static int clamp(int n,int lo,int hi){return n<lo?lo:n>hi?hi:n;}
static int wave(uint32_t t,unsigned period,int amp){int p=t%period;return p<(int)period/2?-amp+4*amp*p/(int)period:3*amp-4*amp*p/(int)period;}
static void rect(orbit_frame_t *f,int x,int y,int w,int h,int color,int opacity){
    if(w<=0||h<=0||opacity<=0||f->count>=ORBIT_MAX_PRIMITIVES)return;
    f->items[f->count++]=(orbit_primitive_t){0,x,y,w,h,0,0,color,opacity};
}
static bool bit(uint16_t row,int col,bool mirror){return (row & (1U<<(mirror?col:8-col)))!=0;}
static void sprite(orbit_frame_t *f,const uint16_t *rows,int n,int cx,int cy,int height,bool mirror,int color){
    const int cell=6,x=cx-27,y=cy-height/2;
    int grid_color=0xA9CCD3;
    for(int r=0;r<n;r++){
        int top=y+r*height/n,bottom=y+(r+1)*height/n;
        if(bottom<=top)continue;
        for(int c=0;c<9;c++)if(bit(rows[r],c,mirror)){
            rect(f,x+c*cell,top,cell,bottom-top,grid_color,255);
            rect(f,x+c*cell+1,top+(bottom-top>2?1:0),cell-1,bottom-top-(bottom-top>2?1:0),color,255);
        }
    }
}
/* Row spans keep accessory geometry within the LVGL object budget. */
static void scaled_glyph(orbit_frame_t *f,const uint8_t *rows,int n,int x,int y,int scale,int color){
    for(int r=0;r<n;r++)for(int c=0;c<5;){
        if(!(rows[r]&(1U<<(4-c)))){c++;continue;}
        int start=c;while(c<5 && (rows[r]&(1U<<(4-c))))c++;
        rect(f,x+start*scale,y+r*scale,(c-start)*scale,scale,color,230);
    }
}
static void glyph(orbit_frame_t *f,const uint8_t *rows,int n,int x,int y,int color){
    scaled_glyph(f,rows,n,x,y,2,color);
}
static void teardrop(orbit_frame_t *f,int x,int y,int color){
    /* Narrow tip, broad rounded belly, tapered base; never a plus sign. */
    rect(f,x+3,y,1,2,color,230);
    rect(f,x+2,y+2,3,2,color,230);
    rect(f,x+1,y+4,5,2,color,230);
    rect(f,x,y+6,7,3,color,230);
    rect(f,x+1,y+9,5,1,color,230);
    rect(f,x+2,y+10,3,1,color,230);
}
static void eyebrow(orbit_frame_t *f,int cx,int y,bool rising,int color){
    for(int i=0;i<8;i++)rect(f,cx-24+i*6,y+(rising?7-i:i)*2,6,3,color,255);
}
void pixel_frame(orbit_id_t id,unsigned eye,uint32_t t,uint32_t duration,unsigned voice,orbit_frame_t *f){
    f->count=0;const int pale=0xD6ECF2;int color=pale;
    if(id==ORBIT_BOOT){
        unsigned index=t>=4000?95:t*24/1000;
        int cx=64+wakeup[index][eye?2:0],h=wakeup[index][eye?3:1];
        if(h<=3)rect(f,cx-27,52,54,h,color,255);
        else sprite(f,normal,5,cx,53,h,false,color);
        return;
    }
    orbit_pose_t p;orbit_sample(id,eye,t,duration,voice,&p);
    const uint16_t *mask=normal;int rows=5,cx=64+p.gaze,cy=53+(p.y-54),height=30;
    bool mirror=false,blink=p.h<=5 && p.curve==0;
    uint32_t d=duration?duration:orbit_duration(id);
    unsigned progress=d?(unsigned)(((uint64_t)(t<d?t:d)*1000)/d):0;
    switch(id){
    case ORBIT_IDLE:cx=64+wave(t,4400,2);break;
    case ORBIT_LISTENING:mask=wide_eye;rows=6;height=36;cx=64;break;
    case ORBIT_ANALYZING:case ORBIT_THINKING:mask=half;cy=49;cx=70;break;
    case ORBIT_SPEAKING:height=30+wave(t,600,2);break;
    case ORBIT_EXECUTING:mask=half;break;
    case ORBIT_WALKING:cy=53+wave(t,800,2);break;
    case ORBIT_CHARGING:mask=smile;cy=53+wave(t,1000,4);height=26+wave(t,500,4);blink=false;break;
    case ORBIT_LOW_BATTERY:mask=t%3600>2700?closed:half;color=0xAEC4C5;cy=55+wave(t,3600,3);blink=false;break;
    case ORBIT_ERROR:mask=sad;mirror=eye!=0;break;
    case ORBIT_SLEEP:mask=closed;color=0x718D98;cy=55+wave(t,2800,2);break;
    case ORBIT_SUPPORT_PROBE:height=24;cx=64;break;
    case ORBIT_HELLO_SMALL:if(progress>600)mask=smile;break;
    case ORBIT_HELLO:mask=progress>350?smile:normal;cy+=wave(t,1000,2);break;
    case ORBIT_BYE:mask=progress>780?closed:smile;break;
    case ORBIT_RESPOND:mask=progress<350?wide_eye:normal;rows=progress<350?6:5;height=rows*6;cx=64;cy=53+wave(t,840,7);blink=false;break;
    case ORBIT_AFFECTION:mask=wide_eye;rows=6;height=36;break;
    case ORBIT_THANKS:case ORBIT_HAPPY:mask=smile;break;
    case ORBIT_EXCITED:mask=smile;cy=53+wave(t,650,5);break;
    case ORBIT_ANGRY:mask=angry;mirror=eye!=0;break;
    case ORBIT_SULKY:mask=half;break;
    case ORBIT_SAD:mask=sad;mirror=eye!=0;break;
    case ORBIT_DISAPPOINTED:mask=half;cy=55+(int)progress/200;break;
    case ORBIT_SHY:mask=half;break;
    case ORBIT_SURPRISED:mask=round_eye;rows=7;height=t<200?30+(int)t*12/200:42;cx=64;break;
    case ORBIT_NERVOUS:mask=wide_eye;rows=6;height=36;break;
    case ORBIT_SLEEPY:height=clamp(30-(int)progress/35,2,30);if(progress>470&&progress<600)height=25;if(progress>850){mask=closed;height=30;}break;
    case ORBIT_CURIOUS:if(eye){mask=half;}else{mask=wide_eye;rows=6;height=36;}break;
    case ORBIT_CONFUSED:if(((eye!=0)^(progress>=500))){mask=wide_eye;rows=6;height=36;}else mask=half;break;
    case ORBIT_PROUD:mask=eye?half:normal;break;
    case ORBIT_PLAYFUL:if(progress>600&&progress<780&&eye)mask=smile;break;
    case ORBIT_RELUCTANT:mask=progress<650?half:normal;break;
    default:break;
    }
    uint32_t natural=t%4300;
    if(p.blink && p.curve==0 && natural>=3400 && natural<3640){
        int q=(int)natural-3520;if(q<0)q=-q;height=clamp(height*q/120,2,height);
    }
    if(blink || height<=3)rect(f,cx-27,cy-1,54,2,color,255);
    else sprite(f,mask,rows,cx,cy,height,mirror,color);
    static const uint8_t heart[]={0x0A,0x1F,0x1F,0x0E,0x04};
    static const uint8_t star[]={0x04,0x04,0x1F,0x04,0x04};
    static const uint8_t question[]={0x0E,0x11,0x02,0x04,0,0x04};
    static const uint8_t zzz[]={0x1F,0x02,0x04,0x08,0x1F};
    static const uint8_t warning[]={0x04,0x04,0x04,0,0x04};
    /* Accessories share the reference pale palette and integer pixel geometry. */
    switch(id){
    case ORBIT_AFFECTION:
        for(int i=0;i<3;i++)scaled_glyph(f,heart,5,eye?97+i*6:21-i*6,
            34-i*11+wave(t+i*280,1600,3),i==0?2:1,color);break;
    case ORBIT_PROUD:if(eye)glyph(f,star,5,104,26+wave(t,900,2),color);break;
    case ORBIT_CONFUSED:if(eye)glyph(f,question,6,104,24,color);break;
    case ORBIT_SAD:
        for(int side=0;side<2;side++)for(int i=0;i<3;i++)
            teardrop(f,side?100:21,51+(int)((t+side*200+i*500)%1500)*33/1500,color);
        break;
    case ORBIT_NERVOUS:
        for(int side=0;side<2;side++)for(int i=0;i<3;i++)
            teardrop(f,side?106:15,12+(int)((t+side*190+i*600)%1800)*66/1800,color);
        break;
    case ORBIT_SLEEP:case ORBIT_SLEEPY:
        for(int i=0;i<3;i++){
            int scale=1+(i+(t/900)%3)%3;
            scaled_glyph(f,zzz,5,eye?96+i*7:22-i*8,40-i*17+wave(t,2800,2),scale,color);
        }break;
    case ORBIT_ERROR:glyph(f,warning,5,eye?104:14,74,color);break;
    case ORBIT_ANGRY:eyebrow(f,cx,cy-32,eye!=0,color);break;
    case ORBIT_CURIOUS:if(eye)eyebrow(f,cx,cy-33,true,color);break;
    case ORBIT_ANALYZING:case ORBIT_THINKING:for(int i=0;i<3;i++)rect(f,51+i*10,83,4,4,color,(t/320)%3==(unsigned)i?255:65);break;
    case ORBIT_SPEAKING:case ORBIT_LISTENING:
        for(int i=0;i<5;i++){int h=2+clamp(id==ORBIT_LISTENING?30:voice,0,255)*(4+wave(t+i*120,700,3))/180;rect(f,48+i*7,86-h/2,3,h,color,180);}break;
    case ORBIT_EXECUTING:case ORBIT_SUPPORT_PROBE:rect(f,44,85,10+(int)(t%1600)*30/1600,2,color,140);break;
    default:break;
    }
    if(id==ORBIT_SHY||id==ORBIT_HAPPY||id==ORBIT_HELLO){
        for(int k=0;k<4;k++)rect(f,(k<2?27:91)+(k%2)*5,78,2,4,color,120+wave(t,2000,40));
    }
}
